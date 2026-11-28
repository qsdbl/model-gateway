"""账号/渠道调度（P4）：可解释、可配置的决策链，不是模糊的"智能优选"。

决策顺序（README 第5节 P4，严格按此实现）：
  1. 能力与状态筛选：授权有效、模型支持本次所需的工具/图片/上下文、未触发限制、参与路由
  2. 费用条件：免费优先；收费兜底必须显式打开
  3. 按配置的模型/平台顺序选渠道（手工排序）
  4. 同一平台、同一候选模型内，按可用积分从高到低选账号
  5. 当前请求及工具往返保持稳定，不因余额刷新频繁换号

关键约束：
- WorkBuddy 与 Qoder 积分是不同计价单位，**分别排序、不互比**（不归一为同一货币）。
- 免费判断带账号/地区/产品/活动时间/更新时间；价格未知不算免费；优惠过期失效。
- 配置变化在当前工具回合结束后生效（通过 tool_round_signature 判定）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import settings
from .context import tool_round_signature


@dataclass
class Candidate:
    """一个可路由候选 = 账号 × 模型。"""
    account_label: str
    platform: str
    model_name: str
    public_name: str | None
    balance: float | None
    balance_unit: str | None
    auth_status: str
    is_free: bool | None
    price_factor: float | None
    promotion_until: float | None
    tools: bool | None
    image: bool | None
    context_window: int | None
    price_fetched_at: float | None
    # --- P34/P35 新增（都有默认值，老调用方不受影响）---
    measured_credit: float | None = None   # 实测这次扣了多少（探针读到的）
    measured_free: bool | None = None      # 实测是否免费；None=未知（不能当免费）
    display_name: str | None = None        # 目录里的中文/英文名
    expires_at: float | None = None        # 凭据到期
    cooldown_until: float | None = None    # 冷却到什么时候
    # --- P41 新增：按 token 计费后，「这个模型免费吗」变成
    # 「**这次这个长度的请求**用它要花多少」。
    est_cost: float | None = None          # 本次请求的预计扣费；None=没有观测数据
    free_upto: int = 0                     # 已知的免费长度上限（token）


@dataclass
class RouteDecision:
    """一次路由决策的完整结果（可解释，写进请求记录）。"""
    candidate: Candidate | None
    reason: str
    considered: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        """写进请求记录的字段。"""
        if not self.candidate:
            return {"platform": None, "model_name": None, "account_label": None,
                    "choose_reason": self.reason}
        c = self.candidate
        return {
            "platform": c.platform,
            "model_name": c.model_name,
            "account_label": c.account_label,
            "choose_reason": self.reason,
        }


class Router:
    """无状态路由：每次调用基于传入的账号/模型快照做决策。

    会话稳定由调用方维护 session_signature → decision 的映射（见 choose 的
    sticky 参数），保证同一工具回合内不换号。
    """

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or (settings.data_dir / "routes.json")
        self._sticky: dict[str, tuple[str, str, str]] = {}  # sig -> (platform, model, account)

    # ---------- 配置 ----------
    # R1 新增：双模式配置。low=免费优先（没免费就降级付费）；high=指定强模型、愿意花积分。
    # 每个模式一套 allow_paid / strategy / model_order；平台顺序和禁用列表全局共用。
    _LOW_PROFILE = {
        "allow_paid": True,      # Q2 拍板：没免费模型就降级付费，不设金额上限
        "strategy": "auto",      # 免费档 → 预计扣费升序 → 轮转摊平
        "model_order": [],
    }
    _HIGH_PROFILE = {
        "allow_paid": True,      # high 本来就要花钱
        "strategy": "ordered",   # 严格按 high_models 优先级
        # Q3 预置：先勾选 DeepSeek 系（真实 id 来自 costmodel/qoder_upstream 目录），用户可再调
        "model_order": ["deepseek-v4.1-flash", "DeepSeek-V4-Pro"],
    }

    def load_config(self) -> dict[str, Any]:
        """读取路由配置：全局项 + low/high 两套模式 profile。

        向后兼容：老配置文件没有 `profiles` 字段时，用它的 allow_paid/model_order/
        strategy 迁移成 low profile，high 用默认值——避免服务器上的旧文件读不了。
        """
        default: dict[str, Any] = {
            "platform_order": ["workbuddy", "qoder"],
            # 顶层键保留：mock 路径的 Router.choose() 仍读它们；双模式调度走 profiles。
            "model_order": [],
            "allow_paid": True,
            "strategy": "auto",
            "free_first": True,
            # R2.6：low 列表的「显示顺序」（纯 UI），与调度策略解耦。
            # 用户拖拽 low 列表只写这里，不碰 strategy/model_order——否则会把
            # low 的免费优先自动调度改成严格顺序，付费模型被拖到前面就开始扣费。
            "ui_low_order": [],
            "enabled_accounts": [],     # 空=全部
            "disabled_models": [],      # 被禁用的模型，调度时直接跳过
            "manual_model": None,       # 旧置顶键（兼容；R2.51 起由 manual_model_low/high 取代）
            # R2.51：low / high 各自独立的置顶模型。旧 manual_model 兼容读取：
            # 没写新键时回退到它，写新键后不再受它影响，避免老用户配置失效。
            "manual_model_low": None,
            "manual_model_high": None,
            # R1 双模式
            "profiles": {
                "low": dict(self._LOW_PROFILE),
                "high": dict(self._HIGH_PROFILE),
            },
            "hidden_models": [          # Q5 之前 Q3：过滤上游的笼统别名（展示+候选池都过滤）
                "auto", "default", "fast-model", "balanced-model", "deep-model",
                "Auto", "Fast", "自动选", "快速", "均衡", "极致",
            ],
            # Q4：high 积分预警阈值（按平台的百分比，不写死数字）
            "low_balance_alert": {
                "enabled": True,
                "ratio": 0.1,           # 余额 < 近 7 日峰值 × ratio 时告警
                "window_days": 7,
            },
            # --- R2.35 轮转公平窗口（分钟）---
            # 调度按「最近 N 分钟用了几次」摊平账号，窗口滑过即归零，
            # 避免历史累计计数固化导致 10 个账号只有 5 个被轮询到。
            # 0 = 退回旧的历史累计口径（可回滚）。
            "rotation_window_minutes": 30,
            # --- P34 / R2.36 探针 ---
            # mode=coldstart：只为「没有价格观测点的新模型」做一次冷启动标定；
            # 后续价格更新完全交给真实请求的 costmodel.observe()（零额外成本）。
            # mode=all：旧行为，定期探 top_k 个模型（保留可回滚）。
            "probe_mode": "coldstart",
            "probe_enabled": True,
            "probe_ttl": 3600,
            "probe_budget_per_hour": 12,
            "probe_top_k": 6,
        }
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                # 旧文件字段 → low profile 迁移（只在没有 profiles 时）
                if "profiles" not in data:
                    data["profiles"] = {
                        "low": {
                            "allow_paid": data.get("allow_paid", True),
                            "strategy": data.get("strategy", "auto"),
                            "model_order": data.get("model_order", []),
                        },
                        "high": dict(self._HIGH_PROFILE),
                    }
                # 两个 profile 都补全默认键（老文件可能只有部分）
                for mode, base in (("low", self._LOW_PROFILE), ("high", self._HIGH_PROFILE)):
                    prof = dict(base)
                    prof.update(data["profiles"].get(mode) or {})
                    data["profiles"][mode] = prof
                default.update(data)
            except Exception:
                pass
        return default

    def mode_config(self, cfg: dict[str, Any] | None = None, mode: str = "low") -> dict[str, Any]:
        """取某个模式的 profile（合并全局项），供调度器使用。"""
        cfg = cfg or self.load_config()
        prof = dict((cfg.get("profiles") or {}).get(mode) or {})
        # 全局项对两模式都可见
        for key in ("platform_order", "free_first", "enabled_accounts",
                    "disabled_models", "hidden_models", "low_balance_alert"):
            prof[key] = cfg.get(key)
        prof["mode"] = mode
        return prof

    def save_config(self, cfg: dict[str, Any]) -> None:
        # R1 兼容层：后台现有 UI 仍读写顶层 allow_paid/model_order/strategy，
        # 而调度器读 profiles.low。保存时把顶层键同步进 low profile，
        # 保证后台改动真正生效（high profile 不动，它有独立的 high_models）。
        profiles = cfg.setdefault("profiles", {})
        low = profiles.setdefault("low", dict(self._LOW_PROFILE))
        for key in ("allow_paid", "strategy", "model_order"):
            if key in cfg:
                low[key] = cfg[key]
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- 免费判定 ----------
    @staticmethod
    def is_free_now(c: Candidate, now: float | None = None) -> bool | None:
        """带时效的免费判定。返回 True/False/None(未知)。

        规则（README P5）：
        - is_free 未知 → None（未知不算免费）
        - 优惠过期 → 失效（不算免费）
        - 免费标记与价格字段冲突 → 返回 None（暂停该渠道免费资格）
        """
        now = now if now is not None else time.time()
        if c.is_free is None:
            return None
        # 优惠截止时间存在且已过 → 不再算免费
        if c.promotion_until is not None and c.promotion_until < now:
            return False
        # 免费标记为真但价格倍率>1 且无有效优惠 → 冲突，未知
        if c.is_free and c.price_factor is not None and c.price_factor > 1:
            if c.promotion_until is None or c.promotion_until < now:
                return None
        return bool(c.is_free)

    # ---------- 决策链 ----------
    def choose(
        self,
        candidates: list[Candidate],
        *,
        need_tools: bool = False,
        need_image: bool = False,
        need_context: int = 0,
        messages: list[dict[str, Any]] | None = None,
        sticky: bool = True,
        manual_model: str | None = None,
    ) -> RouteDecision:
        cfg = self.load_config()
        # R1：mock 路径沿用顶层键（旧测试/旧语义改 cfg["allow_paid"] 仍生效）。
        # 双模式的 profile 只在 scheduler.build_plan 路径上用，不覆盖这里的顶层键。
        now = time.time()
        decision = RouteDecision(candidate=None, reason="")

        # ---- 第1步：能力与状态筛选 ----
        capable: list[Candidate] = []
        for c in candidates:
            if c.auth_status != "ok":
                decision.rejected.append((c.account_label, f"auth_status={c.auth_status}"))
                continue
            if cfg["enabled_accounts"] and c.account_label not in cfg["enabled_accounts"]:
                decision.rejected.append((c.account_label, "account not in enabled_accounts"))
                continue
            if need_tools and c.tools is not True:
                decision.rejected.append((c.account_label, f"tools unsupported on {c.model_name}"))
                continue
            if need_image and c.image is not True:
                decision.rejected.append((c.account_label, f"image unsupported on {c.model_name}"))
                continue
            if need_context and (c.context_window is None or c.context_window < need_context):
                decision.rejected.append((c.account_label, f"context {c.context_window} < {need_context}"))
                continue
            # 价格查询失败/未知不直接淘汰，但标记供第2步费用判断
            capable.append(c)

        if not capable:
            decision.reason = "no capable account (auth/ability/scope filtered)"
            return decision

        # ---- 会话稳定：同一工具回合沿用上次决策 ----
        if sticky and messages is not None:
            sig = tool_round_signature(messages)
            prev = self._sticky.get(sig)
            if prev:
                pp, pm, pa = prev
                for c in capable:
                    if (c.platform, c.model_name, c.account_label) == (pp, pm, pa):
                        # 上次账号仍可用，直接沿用；不因余额刷新换号
                        decision.candidate = c
                        decision.reason = f"sticky within tool round (sig={sig})"
                        return decision

        # ---- 手动指定模型（P15）：只保留该模型，且视为「显式同意收费」----
        manual = False
        if manual_model:
            picked = [c for c in capable if c.model_name == manual_model]
            if picked:
                capable = picked
                manual = True

        # ---- 第2步：费用条件 ----
        free_pool = [c for c in capable if self.is_free_now(c, now) is True]
        paid_pool = [c for c in capable if self.is_free_now(c, now) is False]
        if manual:
            # 用户在后台亲手点了这个模型，等于显式允许其计费，不再走免费优先
            pool = capable
            fee_reason = f"manual_model({manual_model})"
        elif cfg["free_first"] and free_pool:
            pool = free_pool
            fee_reason = "free_first"
        elif cfg["allow_paid"] and (free_pool or paid_pool):
            # 收费兜底：免费未知也算候选，但需显式允许
            pool = free_pool + paid_pool + [
                c for c in capable if self.is_free_now(c, now) is None
            ]
            fee_reason = "paid_allowed_fallback"
        elif cfg["allow_paid"]:
            pool = capable
            fee_reason = "paid_allowed_all"
        else:
            # 面向人的中文原因（后台「记录」页直接展示，不再是内部英文串）
            decision.reason = "没有可用的免费模型，且未开启付费兜底（需在模型页手动切换）"
            return decision

        # ---- 第3步：手工渠道排序（平台 + 模型） ----
        p_order = {p: i for i, p in enumerate(cfg["platform_order"])}
        m_order = {m: i for i, m in enumerate(cfg["model_order"])}
        pool = sorted(
            pool,
            key=lambda c: (
                p_order.get(c.platform, 9999),
                m_order.get(c.model_name, 9999) if cfg["model_order"] else 0,
            ),
        )

        # ---- 第4步：同平台同模型内按余额降序（分平台单位，不跨比） ----
        grouped: dict[tuple[str, str], list[Candidate]] = {}
        for c in pool:
            grouped.setdefault((c.platform, c.model_name), []).append(c)
        ordered: list[Candidate] = []
        for key in sorted(grouped, key=lambda k: (
            p_order.get(k[0], 9999),
            m_order.get(k[1], 9999) if cfg["model_order"] else 0,
        )):
            group = sorted(
                grouped[key],
                key=lambda c: (-c.balance if c.balance is not None else float("inf")),
            )
            ordered.extend(group)

        decision.candidate = ordered[0]
        c = ordered[0]
        decision.reason = (
            f"{fee_reason} → platform_order/model_order → "
            f"balance_desc_within({c.platform}/{c.balance_unit}) "
            f"balance={c.balance}"
        )
        decision.considered = [
            {"platform": x.platform, "model": x.model_name, "account": x.account_label,
             "balance": x.balance, "free": self.is_free_now(x, now)}
            for x in ordered
        ]

        # 记录会话稳定映射（供同一工具回合复用）
        if sticky and messages is not None:
            sig = tool_round_signature(messages)
            self._sticky[sig] = (c.platform, c.model_name, c.account_label)

        return decision
