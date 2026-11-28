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

import re
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


def _candidate_pool(
    est_tokens: int = 0, *, ignore_cooldowns: bool = False,
) -> tuple[list[Candidate], list[dict[str, str]]]:
    """构造 (账号 × 模型) 的候选池。返回 (候选, 已排除项)。

    `est_tokens` 是本次请求的预计输入 token 数，用来算每个模型的**预计扣费**
    （P41：上游按 token 计费，同一个模型对不同长度价格完全不同）。

    R2.49：`ignore_cooldowns=True` 只用于**免费池容量分母**——算「这个池子本来
    能容纳多少 账号×免费模型」时要忽略临时冷却，否则分子分母取自同一份池子，
    账号一冷却分母就跟着缩水，永远显示 N/N 满格，掩盖了「少了一个账号」。
    真实调度绝不能开这个开关（冷却账号必须剔除）。
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

    _cd_rows = store.list_cooldowns()
    cooldowns = {
        (c["account_label"], c["model_name"]): float(c["until_at"])
        for c in _cd_rows
    }
    # R2.50：容量分母基准池要区分临时冷却与 6004 长期额度耗尽，带上 reason。
    cooldown_reasons = {
        (c["account_label"], c["model_name"]): (c.get("reason") or "")
        for c in _cd_rows
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
            # R2.44：401 与其他失败分开写。以前一律「读不到账单（凭据可能过期）」，
            # 用户被告知去点「重新授权」——但同名账号积分用完 / 临时 429 时根本
            # 不该重新授权，反过来说 401 时又没人明确告诉他要扫码。
            from .app import billing_error_is_auth
            if billing_error_is_auth(label):
                rejected.append({
                    "account": label, "model": "*",
                    "reason": "凭据已失效（账单 401），需重新扫码授权",
                })
            else:
                rejected.append({
                    "account": label, "model": "*",
                    "reason": "读不到账单（上游暂时失败，稍后自动重试）",
                })
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
            cooling = bool(until and until > now)
            if cooling:
                long_cd = _is_long_cooldown(
                    cooldown_reasons.get((label, mid), ""), until, now)
                # R2.50：真实调度一律剔除冷却项；基准池（ignore_cooldowns）
                # 只放行临时冷却，6004/超长冷却仍剔除——否则额度已用完到次日的
                # 账号被算进容量，2/2 会虚报成 2/3。
                if not ignore_cooldowns or long_cd:
                    rejected.append({
                        "account": label, "model": mid,
                        "reason": f"冷却中（到 {time.strftime('%H:%M', time.localtime(until))}）",
                    })
                    continue
            # R2.49：ignore_cooldowns 时临时冷却项仍进池，打上 cooldown_until；
            # 上层用它算「容量分母」。6004 长期冷却已在上面 continue 剔除。
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
                long_cd = _is_long_cooldown(
                    cooldown_reasons.get((label, mid), ""), until, now)
                # R2.50：与 WB 分支同口径——基准池只忽略临时冷却。
                if not ignore_cooldowns or long_cd:
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


def _is_long_cooldown(reason: str, until_at: float, now: float | None = None) -> bool:
    """是否「长期额度耗尽」冷却（区别于临时超时/连接重置）。

    R2.50：免费池容量分母的基准池（`ignore_cooldowns=True`）要忽略**临时**
    冷却（超时/连接重置，通常 5 分钟），才算得出「池子本来有几个账号」；
    但 429 code 6004 是当天额度真的用完到次日，这账号今天确实不能再提供
    免费模型——把它算进基准容量会把「2/2 满格」错误显示成「2/3」。
    冷却超过 6 小时也一并视为长期（上游带重置时刻的那种）。
    """
    now = time.time() if now is None else now
    if "6004" in (reason or ""):
        return True
    return (float(until_at) - now) > 6 * 3600


# 「能力不符」类原因：跟账号状态无关，换个账号也一样被剔除。
# 旧实现按候选池顺序取前 3 条账号，平台顺序靠前的账号级 401 会把这类原因挤没，
# 于是 503 只报「某某账号凭据失效」，用户去重新授权，回来照样失败。
_CAPABILITY_REASONS = (
    "不支持工具调用", "不支持图片输入", "上下文", "已在 hidden_models", "模型已被禁用",
)

_PLATFORM_NAMES = {"workbuddy": "WorkBuddy", "qoder": "Qoder", "mock": "本地模拟"}

# 这类剔除是我们**故意**做的（auto/快速/均衡这些笼统别名本来就不进候选池），
# 不是「没有候选」的原因，写进 503 只会让人以为出事了。
_SILENT_REASONS = ("已在 hidden_models",)


def _plain_account(acc: str) -> str:
    """label → 用户可读名；跨平台可能同名，带平台前缀区分。"""
    from .display import display_label

    if ":" in acc:
        plat, _, bare = acc.partition(":")
        return f"{_PLATFORM_NAMES.get(plat, plat)} {display_label(bare)}"
    return display_label(acc)


def _summarize_rejected(rejected: list[dict[str, str]]) -> str:
    """把剔除原因压成「能力不符 + 账号状态」两段人话。

    - **能力类**（tools/image/context/禁用）按「平台 + 原因」聚合计数，
      排在最前面——它解释「为什么这次请求一个候选都没有」；
    - **账号状态类**（401/积分用完/冷却/未授权）按账号聚合，同原因的账号
      并成一行，最多 2 组，避免同一句话重复多遍。
    """
    account_reasons: dict[str, str] = {}       # 账号 → 原因（同账号只留一条）
    capability: dict[tuple[str, str], int] = {}  # (平台, 原因) → 候选数
    for r in rejected or []:
        acc = str(r.get("account") or "?").strip()
        reason = str(r.get("reason") or "").strip()
        if not reason or any(k in reason for k in _SILENT_REASONS):
            continue
        if any(k in reason for k in _CAPABILITY_REASONS):
            plat = acc.split(":", 1)[0] if ":" in acc else "workbuddy"
            capability[(plat, reason)] = capability.get((plat, reason), 0) + 1
            continue
        # 账号级（model="*"）优先于模型级：前者才是「整个号不能用」的根因
        if acc in account_reasons and r.get("model") != "*":
            continue
        account_reasons[acc] = reason

    parts: list[str] = []
    for (plat, reason), n in sorted(capability.items(), key=lambda kv: -kv[1])[:2]:
        name = _PLATFORM_NAMES.get(plat, plat)
        parts.append(f"{name} {n} 个候选{reason}")

    by_reason: dict[str, list[str]] = {}
    for acc, reason in account_reasons.items():
        by_reason.setdefault(reason, []).append(acc)
    for reason, accs in sorted(by_reason.items(), key=lambda kv: -len(kv[1]))[:2]:
        shown = [_plain_account(a) for a in accs[:4]]
        who = "、".join(shown) + (f" 等 {len(accs)} 个" if len(accs) > 4 else "")
        parts.append(f"{who} {reason}")
    return "；".join(parts)


def _locked_unavailable_reason(locked_acc: str, rejected: list[dict[str, str]]) -> str:
    """R2.44：把「锁定账号为什么不可用」翻译成人话。

    只看这个账号自己的被剔除记录（别的账号的原因和它无关），按严重程度取一条：
    凭据失效 > 账号停用 > 余额 0（+冷却）> 冷却中 > 未知。
    以前统一写成「读不到账单/已停用，请重新授权」，把「积分用完」「429 冷却」
    全指向了扫码，用户当然觉得系统在瞎报。
    """
    mine = [r for r in rejected if r.get("account") == locked_acc]
    if not mine:
        return "账号不在网关账号列表里（凭据文件可能被删了）"
    reasons = [str(r.get("reason") or "") for r in mine]
    joined = " | ".join(reasons)
    if "停用" in joined:
        return "账号已在后台停用"
    if "401" in joined or "凭据" in joined:
        return "凭据已失效（上游 401），需要重新扫码授权"
    no_balance = "余额为 0" in joined
    cooldown = next((s for s in reasons if "冷却中" in s), "")
    cool_at = _cooldown_time(cooldown)
    if no_balance and cooldown:
        return (f"积分已用完（余额 0），付费模型不能用；"
                f"免费模型额度也耗尽，{cool_at} 才恢复")
    if no_balance:
        return "积分已用完（余额 0），付费模型不能用（免费模型可能仍在冷却）"
    if cooldown:
        return f"免费模型额度用完（{cool_at} 恢复），付费模型未启用"
    return "、".join(dict.fromkeys(reasons))[:120] or "没有可用模型"


def _cooldown_time(reason: str) -> str:
    """从「冷却中（到 13:04）」里取出「13:04」；取不到就退回原句。"""
    m = re.search(r"到\s*([0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)", reason or "")
    return m.group(1) if m else (reason or "稍后").strip()



def free_fallback_summary(rejected: list[dict[str, str]]) -> list[str]:
    """R2.39：从被剔除项里挑出「真免费池模型为何没用上」，给扣费邮件用。

    只认官方免费池成员（`freepool.is_official_free`），付费模型短请求免扣不算。
    同一个模型多行只留一条（按账号去重），输出形如
    `hy4flash-free@某账号：冷却中（到 14:20）`。
    """
    seen: set[tuple[str, str]] = set()
    out: list[str] = []
    for r in rejected or []:
        mid = r.get("model")
        if not mid or not _is_free_pool_by_name(mid):
            continue
        key = (mid, r.get("account") or "")
        if key in seen:
            continue
        seen.add(key)
        reason = (r.get("reason") or "不可用").strip()
        # R2.50：账号走显示名（可能是别名），进邮件前先脱敏。
        out.append(f"{mid}@{_plain_account(r.get('account') or '?')}：{reason}")
    return out[:6]   # 邮件里别塞太长，最多 6 条


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

    # R2.39：手动「切换到此账号」锁要对预览生效。
    # 真实请求走 app.get_candidates() 会读 modelstate.active_account() 把候选
    # 限制到那一个账号；但预览（_candidate_pool）以前不读，导致切了账号后
    # 「当前选中 · 自动调度预览」仍显示调度器自己挑的账号，看不到切换效果。
    # 这里对齐同口径：只 workbuddy 真实模式读锁，mock/测试不受影响。
    try:
        from .config import settings as _settings
        if _settings.upstream == "workbuddy":
            from . import modelstate
            locked_acc = modelstate.active_account()
            if locked_acc:
                kept_acc = [c for c in pool if c.account_label == locked_acc]
                if kept_acc:
                    for c in pool:
                        if c.account_label != locked_acc:
                            rejected.append({
                                "account": c.account_label, "model": c.model_name,
                                "reason": "已锁定其他账号",
                            })
                    pool = kept_acc
                    notes.append("手动账号：" + locked_acc)
                else:
                    # R2.43：锁定账号不在候选池里（账单读不到/被停用）→ 直接不服务，
                    # 不再静默回退到别的账号。用户既然锁定了，就只走这一个；
                    # 它不可用就该明确失败并提示重新授权，而不是「页面显示已锁定、
                    # 日志却走别的号」的假象（这正是用户报告的 bug）。
                    #
                    # R2.44：原因必须说准。以前一律说「读不到账单/已停用，请重新授权」，
                    # 实际最常见的是「积分用完（余额 0）」和「免费模型 429 冷却中」——
                    # 这两种跟授权一点关系都没有，让用户去扫码是纯粹的误导。
                    why = _locked_unavailable_reason(locked_acc, rejected)
                    if "401" in why or "凭据" in why:
                        tail = "请到账号页点「重新授权」重新扫码，或点「恢复自动调度」用其他账号。"
                    elif "停用" in why:
                        tail = "请到账号页启用它，或点「恢复自动调度」用其他账号。"
                    else:
                        tail = "点「恢复自动调度」可改用其他账号；积分/免费额度恢复后自动可用。"
                    for c in pool:
                        rejected.append({
                            "account": c.account_label, "model": c.model_name,
                            "reason": f"锁定的账号 {locked_acc} 当前不可用，未回退到本账号",
                        })
                    return Plan(
                        candidates=[], rejected=rejected,
                        reason=f"已锁定的账号 {locked_acc} 当前不可用：{why}。{tail}",
                    )
    except Exception:
        pass

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
        # R2.44：把「为什么一个候选都没有」直接写进 reason。
        # 以前只说「账号、模型能力或余额都被过滤掉了」，用户只能看到一句正确的废话，
        # 实际原因是「153/173/134 凭据 401、176 余额 0、免费模型冷却中」。
        detail = _summarize_rejected(rejected)
        return Plan(candidates=[], rejected=rejected,
                    reason=("没有可用候选（账号、模型能力或余额都被过滤掉了）"
                            + (f"：{detail}" if detail else "")))

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
        # R2.50：reason 会原样进扣费邮件「为何选它」，必须用显示名，
        # 不能把内部 label 泄露给用户（此前只脱敏了「账号」字段）。
        reason += f" → {top.model_name}@{_plain_account(top.account_label)}（{cost_text}）"
    return Plan(candidates=ordered, rejected=rejected, reason=reason, notes=notes)


def note_used(candidate: Candidate) -> None:
    """选中之后记一次轮转计数（下次同模型优先换一个账号）。"""
    from .store import store

    try:
        store.bump_rotation(candidate.account_label, candidate.model_name)
    except Exception:
        pass


def preview_plan(est_tokens: int = 200, mode: str = "low") -> Plan:
    """当前调度的完整计划（含 reason / rejected），不发请求、不改状态。

    R2.44：`preview_candidate()` 只给 best，页面拿不到「为什么没有 best」，
    于是没有候选时静默退回默认模型，把 hy4flash-free 显示成「使用中」——
    实际一个候选都没有。后台要用 reason 显示真实状态，所以把 Plan 透出来。
    """
    try:
        from .app import router

        cfg = router.load_config()
        return build_plan(est_tokens=est_tokens,
                          manual_model=cfg.get("manual_model") or None,
                          mode=mode)
    except Exception:
        return Plan()


def preview_candidate(est_tokens: int = 200, mode: str = "low"):
    """当前调度器**会选**的候选对象（含账号），不发请求、不改状态。

    P49：以前只暴露 `preview_model()`（模型名），页面想显示"这次会用哪个账号"
    只能另想办法（顶栏曾按「余额最多」挑一个账号，和调度实际选择经常不是同一个
    账号）。这里把整个候选一起给出去，模型和账号口径才是一体的。

    R1：mode=low/high 分别预览两种模式的当前选择（后台要分别展示）。
    """
    return preview_plan(est_tokens=est_tokens, mode=mode).best


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
    # R2.49：免费模型撞 429 code 6004（额度用完）→ 当天首次发独立提醒。
    # 只认 6004（时段配额耗尽），普通超时/连接重置不打扰用户；
    # 只在官方免费池模型上触发（付费模型的 429 走其他口径）。
    # 邮件是旁路，任何异常都不能影响冷却/调度。
    if "6004" in (reason or "") and _is_free_pool(candidate):
        try:
            from . import notify
            notify.note_free_exhausted(
                account=candidate.account_label,
                model=candidate.model_name,
                platform=candidate.platform,
                reset_at=until_at,
                reason=reason,
            )
        except Exception:
            pass
