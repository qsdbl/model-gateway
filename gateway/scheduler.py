"""调度器（P35/P41）：账号 × 模型的二维候选池 + 自动换号。

和旧 `router.choose` 的差别（旧逻辑的问题写在 docs/plan-scheduling-and-selfaware.md）：
- 候选集从「当前一个模型 × 各账号」变成「各账号 × 各候选模型」；
- 同档位不再「余额最多的先用」（总是打爆同一个号），改成**轮转摊平**；
- 上游 429/5xx 会**自动冷却并换号重试**，而不是把错误直接甩给客户端。

**P41：排序维度换了。** P34–P37 以为上游是「免费模型 / 付费模型」二分，
所以按「实测是否免费 → 目录倍率」排序。线上实测推翻了这一点：

| 模型 | 114tok | 414tok | 1214tok | 2514tok | 5014tok |
|---|---|---|---|---|---|
| hy4flash-free | 0 | 0 | 0 | 0 | 0 |
| deepseek-v4.1-flash | 0.01 | 0.01 | 0.01 | 0.01 | 0.01 |
| hy4flash | 0 | 0.01 | 0.02 | 0.04 | 0.08 |
| kimi-k3-1 | 0.15 | 0.31 | 0.62 | 0.96 | 1.57 |

上游是**按请求输入 token 计费**：每个模型有一段免费长度区间，超出后按量收费。
所以「这个模型免费吗」是个没有答案的问题——**「这次这个长度的请求，用这个模型
要花多少」才是**。调度器据此排序：

    排序规则（auto 模式，默认）：
        本次请求的**预计扣费**升序（0 → 0.01 → 0.02 → …）
        同一个模型内按账号轮转次数升序（摊平，别总打爆一个号）

    排序规则（ordered 模式，后台拖拽后启用）：
        严格按后台的模型顺序；列表里没列的模型按预计扣费排到后面。

两种模式共用两条规则：
- **被禁用的模型直接跳过**，无论它在不在手动列表里；
- **手动指定的模型置顶**（P40），但只影响「谁先试」，其余候选仍留作兜底。

「免费用完自动切付费」在新模型下不再需要特殊分支：预计扣费 0 的模型自然排最前，
没有免费的就会选最便宜的——**这本来就是用户想要的行为**。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .config import settings
from .router import Candidate

# 排序档位（P41：按「本次预计扣费」而不是「模型是不是免费」）
TIER_FREE = 0       # 本次预计扣费 = 0
TIER_PAID = 1       # 本次预计扣费 > 0（已知）
TIER_UNKNOWN = 2    # 不知道（没观测数据）


@dataclass
class Plan:
    """一次调度的完整结果。"""
    candidates: list[Candidate] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    reason: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


def _candidate_pool(est_tokens: int = 0) -> tuple[list[Candidate], list[dict[str, str]]]:
    """构造 (账号 × 模型) 的候选池。返回 (候选, 已排除项)。

    `est_tokens` 是本次请求的预计输入 token 数，用来算每个模型的**预计扣费**
    （P41：上游按 token 计费，同一个模型对不同长度价格完全不同）。
    """
    from . import costmodel
    from .app import _account_snapshot
    from .store import store
    from .workbuddy_upstream import catalog, known_accounts

    rejected: list[dict[str, str]] = []
    out: list[Candidate] = []

    db_accounts = {r["label"]: r for r in store.list_accounts()}
    accounts = known_accounts()

    # Qoder 的账号/模型表先取（下面要用）。
    # 注意：**不能**因为 WorkBuddy 一个账号都没有就提前返回 ——
    # 只有 Qoder 账号的部署也必须能出候选（P44 修掉的 bug）。
    try:
        from . import qoder_upstream as qu
        q_accounts = qu.known_accounts()
        q_models = [m for m in qu.catalog() if (m.get("kind") or "chat") == "chat"]
    except Exception:
        q_accounts, q_models = {}, []

    if not accounts and not q_accounts:
        return out, rejected

    models = [m for m in catalog() if (m.get("kind") or "chat") == "chat"]
    if not models and not q_models:
        return out, rejected

    cooldowns = {
        (c["account_label"], c["model_name"]): float(c["until_at"])
        for c in store.list_cooldowns()
    }
    now = time.time()

    # P41：价格曲线按模型缓存一次（同一模型对各账号同价，实测已验证）
    curves = costmodel.all_curves()

    for label, _path in accounts.items():
        row = db_accounts.get(label)
        if row is not None and not row.get("enabled"):
            rejected.append({"account": label, "model": "*", "reason": "账号已在后台停用"})
            continue
        billing, meta = _account_snapshot(label)
        if not billing:
            rejected.append({"account": label, "model": "*", "reason": "读不到账单（凭据可能过期）"})
            continue
        balance = billing.get("total")
        no_balance = balance is not None and float(balance) <= 0
        expires_at = (meta or {}).get("expires_at")
        for m in models:
            mid = m.get("model_name") or ""
            if not mid:
                continue
            # R2.1：余额≤0 只剔除**付费模型**——官方免费模型(pricing=free)不消耗
            # 付费积分，余额为 0 照样能用（服务器实调 176+hy4flash-free 验证 credit=0）。
            # 旧代码在账号级一刀切 continue，把免费模型也误伤，白少一个免费账号。
            if no_balance and not _is_free_pool_by_name(mid):
                rejected.append({"account": label, "model": mid,
                                 "reason": "余额为 0（付费模型需积分）"})
                continue
            until = cooldowns.get((label, mid))
            if until and until > now:
                rejected.append({
                    "account": label, "model": mid,
                    "reason": f"冷却中（到 {time.strftime('%H:%M', time.localtime(until))}）",
                })
                continue
            curve = curves.get(mid)
            est_cost = curve.cost_at(est_tokens) if curve else None
            out.append(Candidate(
                account_label=label,
                platform="workbuddy",
                model_name=mid,
                public_name=settings.public_model,
                balance=balance,
                balance_unit="credits_workbuddy",
                auth_status="ok",
                is_free=bool(m.get("is_free")) if m.get("credits_factor") is not None else None,
                price_factor=m.get("credits_factor"),
                promotion_until=None,
                tools=bool(m.get("tools", True)),
                image=bool(m.get("image", False)),
                context_window=m.get("context_window") or settings.context_window or 128000,
                price_fetched_at=billing.get("checked_at"),
                measured_credit=curve.measured_min_credit if curve else None,
                measured_free=(curve.free_upto > 0) if curve else None,
                display_name=m.get("display_name") or mid,
                expires_at=expires_at,
                cooldown_until=until,
                est_cost=est_cost,
                free_upto=curve.free_upto if curve else 0,
            ))

    # ---- Qoder 候选（P44）----
    #
    # Qoder 的积分和 WorkBuddy 的积分**不是同一种货币**（router.py 的硬约束），
    # 所以这里不做任何跨平台的价格比较：每个平台内部按自己的规则排，
    # 平台之间按 routes.json 的 platform_order 排（默认 WorkBuddy 优先）。
    #
    # 准入条件比 WorkBuddy 严一档：**必须有 CLI 本地登录态**——
    # Qoder 的推理靠 `qoderclicn` 子进程，环境变量塞 dt- 会被完全忽略，
    # 每个账号必须各自授权过一次。
    for label, _path in q_accounts.items():
        row = db_accounts.get(label)
        if row is not None and not row.get("enabled"):
            rejected.append({"account": label, "model": "*", "reason": "账号已在后台停用"})
            continue
        try:
            auth = qu.auth_state(label)
        except Exception:
            auth = {"authorized": False, "reason": "检查授权状态失败"}
        if not auth.get("authorized"):
            rejected.append({
                "account": label, "model": "*",
                "reason": auth.get("reason") or "未授权（点「授权」走一次设备码登录）",
            })
            continue
        # 余额取库里那份（签到系统每天同步 + 后台「检查授权」时刷新）。
        # 这里不打上游：调度在每个请求的关键路径上，不能为了个余额去发 HTTP。
        balance = row.get("balance")
        if balance is not None and float(balance) <= 0:
            rejected.append({"account": label, "model": "*", "reason": "余额为 0"})
            continue
        for m in q_models:
            mid = m.get("model_name") or ""
            if not mid:
                continue
            until = cooldowns.get((label, mid))
            if until and until > now:
                rejected.append({
                    "account": label, "model": mid,
                    "reason": f"冷却中（到 {time.strftime('%H:%M', time.localtime(until))}）",
                })
                continue
            out.append(Candidate(
                account_label=label,
                platform="qoder",
                model_name=mid,
                public_name=settings.public_model,
                balance=(float(balance) if balance is not None else None),
                balance_unit="credits_qoder",
                auth_status="ok",
                # Qoder 的费率没标定过 → 免费与否、预计扣费都**未知**。
                # 「未知」不能当免费（P41 红线），所以 est_cost 留 None。
                is_free=None,
                price_factor=None,
                promotion_until=None,
                tools=False,          # CLI 的 agent 工具被我们关了（--tools ""）
                image=False,
                context_window=m.get("context_window") or 128000,
                price_fetched_at=row.get("balance_checked_at"),
                measured_credit=None,
                measured_free=None,
                display_name=m.get("display_name") or mid,
                expires_at=None,
                cooldown_until=until,
                est_cost=None,
                free_upto=0,
            ))
    return out, rejected


def _is_free_pool_by_name(model_name: str | None) -> bool:
    """按模型名判断官方免费池身份（候选池构造阶段还没有 Candidate 对象）。"""
    from .freepool import is_official_free

    return is_official_free(model_name)


def _is_free_pool(c: Candidate) -> bool:
    """免费池身份（R1.5）：只认官方标 free 的模型，不看本次扣费。

    用户拍板：space-bunny/hunyuan-chat 这种 0.08x 付费模型即使在极短请求下
    实测扣 0，也**不属于免费池**。免费池身份是模型的固定属性，由官方目录决定。
    """
    from .freepool import is_official_free

    return is_official_free(c.model_name)


def _tier(c: Candidate) -> int:
    """本次请求的**免费池档位**（R1.5 统一口径）。

    与旧版的区别：旧版「本次 est_cost<=0 即免费档」会把短请求免扣的付费模型
    （space-bunny 等）误标成免费。现在免费档 = 官方免费池成员，且这次确实不扣费；
    付费模型即使这次 est_cost=0 也归付费档（但排序时仍按 est_cost 升序，
    极短请求下它仍是付费档里最便宜的，省钱效果保留——用户已拍板）。

    返回值仍是 TIER_FREE / TIER_PAID / TIER_UNKNOWN，排序逻辑不用改。
    """
    if c.est_cost is None:
        return TIER_UNKNOWN
    # 官方免费池成员 + 本次不扣费 → 免费档；其余（含短请求免扣的付费模型）→ 付费档
    if _is_free_pool(c) and c.est_cost <= 0:
        return TIER_FREE
    return TIER_PAID


def _cost_key(c: Candidate) -> float:
    """排序用的「价格」：本次预计扣费优先，否则目录倍率。"""
    if c.est_cost is not None:
        return float(c.est_cost)
    if c.price_factor is not None:
        return float(c.price_factor)
    return 9.99


def build_plan(
    *,
    need_tools: bool = False,
    need_image: bool = False,
    need_context: int = 0,
    manual_model: str | None = None,
    est_tokens: int = 0,
    mode: str = "low",
) -> Plan:
    """构造一个有序的候选计划。

    `est_tokens`：本次请求的预计输入 token 数（含事实块）。P41 起，
    排序直接用它算每个模型的预计扣费——这是唯一正确的「谁更便宜」的判据。

    R1 双模式：
    - low：沿用 auto 排序（免费档 → 预计扣费 → 轮转摊平），没免费就降级付费；
    - high：独立排序键——model_order（strong 模型优先级）> 扣费 > 余额摊平 > 账号名，
      平台优先与免费档不再压过模型优先级；账号积分耗尽自然掉出候选池
      （余额≤0 被剔除），自动换下一个账号继续同一模型。
    """
    from .app import router

    cfg = router.load_config()
    # R1：取对应模式的 profile（含全局项），取代旧的扁平配置。
    # getattr 兜底：部分测试/嵌入式调用用 FakeRouter stub（无 mode_config），直接读扁平 cfg。
    _mode_get = getattr(router, "mode_config", None)
    mcfg = (_mode_get(cfg, mode if mode in ("low", "high") else "low")
            if _mode_get else cfg)
    disabled = {str(x) for x in (mcfg.get("disabled_models") or [])}
    strategy = (mcfg.get("strategy") or "auto").lower()
    model_order = [str(x) for x in (mcfg.get("model_order") or [])]
    free_first = bool(mcfg.get("free_first", True))
    allow_paid = bool(mcfg.get("allow_paid", True))
    # Q3/Q5：过滤上游的笼统别名（auto/default/快速/均衡/极致…），展示层+候选池都过滤。
    hidden = {str(x) for x in (mcfg.get("hidden_models") or [])}
    is_high = mode == "high"

    pool, rejected = _candidate_pool(est_tokens)
    notes: list[str] = []

    # R2.37：high 严格平台过滤。用户拍板「配置什么就按什么来，不可用就报错，
    # 不回落其他平台」——所以这里在能力筛选**之前**按平台裁剪候选池，被裁掉的
    # 平台不再参与后续任何兜底（包括 tools 过滤后的回落）。
    # enabled_platforms 空数组/未配置 = 保持旧行为（不过滤），兼容老配置与 low 模式。
    if is_high:
        enabled_platforms = {
            str(x).lower() for x in (mcfg.get("enabled_platforms") or [])
        }
        if enabled_platforms:
            kept: list[Candidate] = []
            for c in pool:
                if str(c.platform).lower() in enabled_platforms:
                    kept.append(c)
                else:
                    rejected.append({
                        "account": c.account_label,
                        "model": c.model_name,
                        "reason": "high 平台未启用",
                    })
            pool = kept
            notes.append("high 严格平台：" + "/".join(sorted(enabled_platforms)))

    # ---- 能力筛选 ----
    capable: list[Candidate] = []
    for c in pool:
        if c.model_name in hidden:
            rejected.append({"account": c.account_label, "model": c.model_name,
                             "reason": "笼统别名（已在 hidden_models 过滤）"})
            continue
        if c.model_name in disabled:
            rejected.append({"account": c.account_label, "model": c.model_name,
                             "reason": "模型已被禁用"})
            continue
        if need_tools and c.tools is not True:
            rejected.append({"account": c.account_label, "model": c.model_name,
                             "reason": "不支持工具调用"})
            continue
        if need_image and c.image is not True:
            rejected.append({"account": c.account_label, "model": c.model_name,
                             "reason": "不支持图片输入"})
            continue
        if need_context and (c.context_window is None or c.context_window < need_context):
            rejected.append({"account": c.account_label, "model": c.model_name,
                             "reason": f"上下文 {c.context_window} < {need_context}"})
            continue
        capable.append(c)

    if not capable:
        return Plan(candidates=[], rejected=rejected,
                    reason="没有可用候选（账号、模型能力或余额都被过滤掉了）")

    # ---- 手动指定模型（P40）：**置顶优先**，不是独占 ----
    #
    # 旧实现是硬覆盖：只保留这一个模型，且它不可用就整个请求失败。
    # 但用户要的是「自动为主、手动兼容」（P39 拍板：默认自动）。
    # 改成置顶后：手动指定的模型排最前；它 429/下线了还能自动退到下一个，
    # 而不是整个服务挂掉。禁用列表仍然优先——禁掉的模型连置顶都不给。
    pinned_model: str | None = None
    if manual_model:
        if any(c.model_name == manual_model for c in capable):
            pinned_model = manual_model
            notes.append(f"手动指定 {manual_model}（置顶）")
        else:
            notes.append(f"手动指定的 {manual_model} 当前不可用，按自动顺序")

    # ---- 费用条件 ----
    # 置顶的模型是用户亲手指定的 = 显式同意计费，不受「免费用完切付费」开关限制；
    # 其余候选照常走免费优先 / 付费兜底。
    pinned_cands = [c for c in capable if pinned_model and c.model_name == pinned_model]
    rest = [c for c in capable if not (pinned_model and c.model_name == pinned_model)]

    if not allow_paid:
        # 「免费用完切付费」关掉了：本次预计要花钱的一律不用，宁可不服务。
        keep: list[Candidate] = []
        for c in rest:
            if _tier(c) == TIER_FREE:
                keep.append(c)
            else:
                rejected.append({"account": c.account_label, "model": c.model_name,
                                 "reason": "本次请求预计收费（未开启付费兜底）"})
        rest_pool = keep
        notes.append("仅用本次免费模型")
    else:
        rest_pool = rest
        if free_first and any(_tier(c) == TIER_FREE for c in rest):
            notes.append("免费优先")
        else:
            notes.append("按预计扣费择优")

    pool2 = pinned_cands + rest_pool

    # R2.17：近期慢候选在**同一档位内**降权。用户明确不做切付费，
    # 所以这个键放在 _tier 之后——慢的免费模型仍排在付费模型前面。
    from . import latency as _latency
    slow_threshold = float(mcfg.get("slow_latency_threshold_s", 90) or 0)
    slow_pairs = [c for c in pool2
                  if _latency.is_slow(c.model_name, c.account_label, slow_threshold)]
    if slow_pairs:
        notes.append(f"{len(slow_pairs)} 个候选近期偏慢（同档位降权，不切付费）")

    if not pool2:
        return Plan(candidates=[], rejected=rejected,
                    reason="没有本次免费的模型，且未开启付费兜底")

    # ---- 排序 ----
    from .store import store

    rotations: dict[str, int] = {}
    for mid in {c.model_name for c in pool2}:
        rotations.update({f"{mid}|{k}": v for k, v in store.rotation_counts(mid).items()})

    order_idx = {m: i for i, m in enumerate(model_order)}
    # P44：平台优先级。Qoder 积分和 WorkBuddy 积分不是同一种货币，**不互比**。
    # low 模式按 platform_order；high 模式把平台降权（用户愿意花钱买指定能力，
    # 不该被平台顺序干扰），但仍作为同模型同成本时的稳定 tie-break。
    p_order = {p: i for i, p in enumerate(mcfg.get("platform_order") or ["workbuddy", "qoder"])}

    def sort_key(c: Candidate) -> tuple:
        # 置顶的手动模型永远第一——只影响「谁先试」，其余候选仍留作兜底。
        if pinned_model and c.model_name == pinned_model:
            model_rank = -1
        elif c.model_name in order_idx and (is_high or strategy == "ordered"):
            model_rank = order_idx[c.model_name]
        else:
            model_rank = 10_000

        if is_high:
            # high：严格模型优先级 → 账号轮转摊平 → 平台 tie-break → 账号名。
            # 关键点：余额≤0 账号的**付费模型**已在候选池被剔除（scheduler.py:116-126）；
            # high 指定的都是付费强模型（deepseek-v4.1-flash / DeepSeek-V4-Pro），
            # 所以「一号积分耗尽 → 换下个账号继续同一模型」仍天然成立。
            # （R2.1 起余额剔除改为模型粒度：只剔付费模型、保留官方免费模型，
            #   免费池不再被余额误伤；high 全是付费模型，不受影响。）
            # 不按成本排——high 是「指定能力强模型」，便宜不该盖过用户指定的优先级。
            return (
                model_rank,
                rotations.get(f"{c.model_name}|{c.account_label}", 0),
                p_order.get(c.platform, 9999),
                c.account_label,
            )
        # low：沿用 auto 排序（免费档 → 预计扣费 → 平台 → 轮转 → 账号名）。
        return (
            model_rank,
            p_order.get(c.platform, 9999),   # 平台优先（积分单位不同，不互比）
            _tier(c),                        # 本次免费 > 已知价 > 未知价
            # R2.17：同档位内把近期慢候选往后放；绝不跨档位切付费。
            1 if _latency.is_slow(c.model_name, c.account_label, slow_threshold) else 0,
            _cost_key(c),                    # 本次预计扣费升序
            rotations.get(f"{c.model_name}|{c.account_label}", 0),  # 轮转摊平
            c.account_label,
        )

    ordered = sorted(pool2, key=sort_key)

    reason = " → ".join(notes) if notes else ("high 按模型优先级" if is_high else "auto")
    if ordered:
        top = ordered[0]
        cost = top.est_cost
        cost_text = (
            f"预计扣费 {cost}" if cost is not None
            else f"价格未知（目录倍率 x{top.price_factor}）" if top.price_factor is not None
            else "价格未知"
        )
        reason += f" → {top.model_name}@{top.account_label}（{cost_text}）"
    return Plan(candidates=ordered, rejected=rejected, reason=reason, notes=notes)


def note_used(candidate: Candidate) -> None:
    """选中之后记一次轮转计数（下次同模型优先换一个账号）。"""
    from .store import store

    try:
        store.bump_rotation(candidate.account_label, candidate.model_name)
    except Exception:
        pass


def preview_candidate(est_tokens: int = 200, mode: str = "low"):
    """当前调度器**会选**的候选对象（含账号），不发请求、不改状态。

    P49：以前只暴露 `preview_model()`（模型名），页面想显示"这次会用哪个账号"
    只能另想办法（顶栏曾按「余额最多」挑一个账号，和调度实际选择经常不是同一个
    账号）。这里把整个候选一起给出去，模型和账号口径才是一体的。

    R1：mode=low/high 分别预览两种模式的当前选择（后台要分别展示）。
    """
    try:
        from .app import router

        cfg = router.load_config()
        plan = build_plan(est_tokens=est_tokens,
                          manual_model=cfg.get("manual_model") or None,
                          mode=mode)
        return plan.best
    except Exception:
        return None


def preview_model(est_tokens: int = 200) -> str | None:
    """当前调度器**会选**的模型（不发请求、不改状态）。

    P40：接入页和状态接口以前显示的是 `effective_model()`——那是 P15 的
    「后台手动锁」或环境变量默认值，跟调度器实际选的可能完全是两回事。
    用户看到「当前上游 A」而实际在跑 B，是最让人困惑的一类不一致。

    P41：上游按 token 计费，所以「会选哪个模型」取决于请求有多长。
    这里用 `est_tokens` 估算（默认 200 = 典型短请求）。真实请求用的是
    「实际长度 + 事实块长度」，所以接入页显示的是「典型情况下会用哪个」。
    """
    cand = preview_candidate(est_tokens)
    return cand.model_name if cand else None


# 可重试的错误特征。**不包含** 400 类的两类：
#   11128「Illegal API invocation from an unapproved channel」= 渠道风控，
#     上游明确 retry_policy.retry=false，换号换模型都没用；
#   11101 Parse message failed = 请求格式问题，重试只会再错一次。
# 这两类历史上有 64 条，占 400 错误的大头，无脑重试就是重试风暴。
_RETRY_MARKERS = (
    "429", "6004", "upstream http 5", "connect failed", "timeout",
    "timed out", "502", "503", "504", "bad gateway", "service unavailable",
    "connection reset", "connection aborted", "remote end closed",
    # R2.15：上游异常断流（IncompleteRead/ConnectionReset 等）包装成 UpstreamError
    # 后带这些字样，必须可重试才能触发换号，否则把 internal_error 甩给客户端。
    "stream broken", "incompleteread", "incomplete read", "remote disconnected",
)


def is_retryable(reason: str) -> bool:
    """这次失败换号重试有没有意义。"""
    if not reason:
        return False
    low = reason.lower()
    if "11128" in low or "11101" in low:
        return False
    if "http 400" in low or "http 401" in low or "http 403" in low:
        return False
    return any(m in low for m in _RETRY_MARKERS)


def cooldown_for(reason: str, default_seconds: float = 300.0) -> float:
    """这次失败该冷却到什么时候（epoch 秒）。

    上游 429 会带重置时间（「将在 2026-11-19 21:45:41 UTC+8 重置」），
    能解析就用它；解析不了就按默认 5 分钟。
    """
    from .probe import parse_reset_time

    at = parse_reset_time(reason)
    if at and at > time.time():
        return at
    return time.time() + default_seconds


def mark_cooldown(candidate: Candidate, until_at: float, reason: str) -> None:
    """把 (账号, 模型) 拉黑到指定时刻。"""
    from .store import store

    try:
        store.set_cooldown(candidate.account_label, candidate.model_name, until_at, reason)
        store.log_schedule(
            "cooldown",
            f"{candidate.account_label}/{candidate.model_name} 冷却到 "
            f"{time.strftime('%m-%d %H:%M', time.localtime(until_at))}：{reason[:120]}",
            "warn",
        )
    except Exception:
        pass
