"""模型自述（P36/P41）：让模型能如实回答「我是谁、倍率多少、额度还剩多少」。

做法是**往 messages 里插一条 system 事实块**，而不是往响应流里塞：
- 塞响应流会破坏 SSE 语义（客户端解析可能出错）；
- 要「检测标识再回填」就得缓冲整段响应，流式实时性全丢；
- 事实块走 system，任何 OpenAI 兼容客户端都吃，模型能自然引用其中的数字。

事实块的内容来自网关自己的状态：当前实际用的模型、实测扣费、账号余额与到期、
可用免费模型清单、倍率档位排序。拿不到就写「未知」，不编。

**P41：事实块分两档（lite / full），这是必须的，不是优化。**

实测结论：上游的免费是**输入长度门槛**（hy4flash ≤123 token 免费，
423 token 起 0.01 且随长度线性涨到 0.08；system 消息也计入 prompt_tokens）。
原来的完整事实块约 391 字符 ≈ 400 token，**它自己就把短请求顶过了门槛**——
用户问一句「你好」也要被扣 0.01，而那本来是免费的。

于是：
- **lite 档**（约 140 token）：只留核心事实（当前模型 / 该模型计费 / 免费模型清单）。
  短请求用它，省下的额度比多写几行账号明细值钱。
- **full 档**（约 400 token）：账号明细、档位排序、停用列表全上。
  **只在用户真的问到模型/倍率/额度/账号时**才用——那种问题本来就值得这点钱。

档位选择在**路由之前**就能定（关键词只看用户消息），所以估算 token、
路由排序、实际注入三者完全一致，不需要迭代，也不存在「模型名对不上」的坑。

开关：`GATEWAY_FACT_PREAMBLE=off` 或后台路由页关掉。
"""
from __future__ import annotations

import os
import time
from typing import Any

from .config import settings

SENTINEL = "【网关事实】"
_TTL = 30.0
_cache: dict[str, Any] = {"at": 0.0, "text": "", "data": None}

# 命中这些词就升级到 full 档：用户在问模型/计费/额度的实情，
# 那时给全量事实比省 0.01 积分有价值。
_ASK_KEYWORDS = (
    "模型", "倍率", "额度", "积分", "余额", "账号", "免费", "收费",
    "扣费", "价格", "收费", "哪个模型", "model", "credit", "quota", "balance",
)

# 各档的 token 估算（含 system 消息的固定开销）。
# 实测：中文一字 ≈ 1 token，英文 ≈ 4 字符 1 token。
# 估高一点无妨（顶多提前选更便宜的模型），估低会漏计费。
MODE_TOKEN_EST = {"off": 0, "lite": 170, "full": 460}


def enabled() -> bool:
    """事实注入开关。默认开。"""
    if os.environ.get("GATEWAY_FACT_PREAMBLE", "").strip().lower() in ("0", "off", "false"):
        return False
    try:
        from .app import router

        cfg = router.load_config()
        return bool(cfg.get("fact_preamble", True))
    except Exception:
        return True


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "未知"
    return time.strftime("%Y-%m-%d", time.localtime(float(ts)))


def collect() -> dict[str, Any]:
    """收集事实数据（带 30 秒缓存，不每个请求都打上游）。"""
    now = time.time()
    if now - float(_cache.get("at") or 0) < _TTL and _cache.get("data") is not None:
        return _cache["data"]  # type: ignore[return-value]

    from . import costmodel
    from .app import _account_snapshot
    from .app import router
    from .store import store
    from .workbuddy_upstream import catalog, known_accounts

    cfg = router.load_config()
    catalog_rows = catalog()
    by_name = {m.get("model_name"): m for m in catalog_rows}
    # P40：active = 调度器当前会选的模型（不是 P15 的遗留手动锁）。
    # 单个请求的事实块会用 render(data, model=chosen.model_name) 覆盖成
    # 这次真正用的那个；这里的 active 用于状态接口和没有指定模型时的兜底。
    active = None
    try:
        from . import scheduler as SCHED
        active = SCHED.preview_model()
    except Exception:
        active = None
    if not active:
        from .workbuddy_upstream import effective_model
        active = effective_model()
    manual = cfg.get("manual_model") or None

    # P41：免费判定改用**价格曲线**（按长度算），不再用「探针测一次 = 免费」。
    # 探针只有一个长度点，测出「22 token 免费」就以为整个模型免费——
    # 实际上 hy4flash 在 423 token 就开始收费了。
    curves = costmodel.all_curves()

    accounts: list[dict[str, Any]] = []
    for label in known_accounts():
        billing, meta = _account_snapshot(label)
        free_models: list[str] = []
        for mid in by_name:
            c = curves.get(mid)
            # 真免费（不限长度）也算——实测 hy4flash-free 30023 token 仍扣 0
            if c and (c.always_free or c.free_upto > 0):
                free_models.append(mid)
        accounts.append({
            "label": label,
            "balance": (billing or {}).get("total"),
            "nickname": (meta or {}).get("nickname") or "",
            "expires_at": (meta or {}).get("expires_at"),
            "free_models": sorted(free_models),
        })

    # 倍率档位：实测优先，目录兜底
    tiers: list[dict[str, Any]] = []
    for m in catalog_rows:
        mid = m.get("model_name") or ""
        if not mid or (m.get("kind") or "chat") != "chat":
            continue
        c = curves.get(mid)
        factor = m.get("credits_factor")
        free_upto = c.free_upto if c else 0
        flat = c.flat_credit if c else None
        tiers.append({
            "model": mid,
            "display_name": m.get("display_name") or mid,
            "factor": factor,
            "measured_credit": min((p.credit for p in c.points), default=None) if c else None,
            "measured_at": None,
            "free": free_upto > 0,
            "free_upto": free_upto,
            "flat_credit": flat,
            "disabled": mid in set(cfg.get("disabled_models") or []),
        })
    # 排序：免费区间最长的排前面，其次按目录倍率
    tiers.sort(key=lambda t: (
        0 if t["free"] else 1,
        -int(t.get("free_upto") or 0),
        t["factor"] if t["factor"] is not None else 9.99,
    ))

    data = {
        "public_model": settings.public_model,
        "active_model": active,
        "manual_model": manual,
        "strategy": cfg.get("strategy", "auto"),
        "free_first": bool(cfg.get("free_first", True)),
        "allow_paid": bool(cfg.get("allow_paid", True)),
        "disabled_models": list(cfg.get("disabled_models") or []),
        "model_order": list(cfg.get("model_order") or []),
        "accounts": accounts,
        "tiers": tiers,
        "free_models": [t["model"] for t in tiers if t["free"]],
        "cooldowns": store.list_cooldowns(),
        "built_at": now,
    }
    _cache.update(at=now, text=render(data), data=data)
    return data


def _cost_line(tier: dict[str, Any] | None, model: str) -> str:
    """这个模型的计费怎么描述（lite/full 都用）。"""
    if not tier:
        return "该模型计费：未知"
    flat = tier.get("flat_credit")
    if flat is not None:
        return f"该模型计费：按次计费，每次 {flat} 积分（不随输入长度变化）"
    free_upto = int(tier.get("free_upto") or 0)
    if free_upto > 0:
        base = f"该模型计费：输入不超过约 {free_upto} token 免费"
        minc = tier.get("measured_credit")
        if minc:
            base += f"，超过后按量计费（实测起步 {minc} 积分）"
        return base
    minc = tier.get("measured_credit")
    if minc is not None:
        return f"该模型计费：实测扣费 {minc} 积分起，按输入长度递增"
    if tier.get("factor") is not None:
        return f"该模型计费：目录倍率 x{tier['factor']}"
    return "该模型计费：未知"


def render(
    data: dict[str, Any],
    model: str | None = None,
    mode: str = "full",
    est_tokens: int | None = None,
) -> str:
    """把事实数据渲染成一段给模型看的文字。

    `model` 显式传入时用它当「当前模型」——P35 之后实际用的模型由调度器决定，
    可能和后台当前选中的那个不是一回事，事实块必须说真话。

    `mode`：
    - "lite"：只留核心事实（约 140 token），短请求用它，别把免费额度顶掉；
    - "full"：完整版（约 400 token），用户真问到模型/额度时才用。
    """
    active = model or data.get("active_model") or "未知"
    tier = next((t for t in data.get("tiers", []) if t["model"] == active), None)

    lines: list[str] = []
    lines.append(
        "【网关事实】回答「你是什么模型 / 倍率多少 / 额度还剩多少 / 有哪些模型可用」"
        "这类问题时，必须直接引用下面的数值，不要说“混元模型”之类的模糊回答。"
    )
    lines.append(f"当前对话实际使用的模型：{active}")
    lines.append(_cost_line(tier, active))

    free_models = data.get("free_models") or []
    if free_models:
        lines.append("当前实测可用的免费模型：" + "、".join(free_models))
    else:
        lines.append("当前没有实测为免费的模型（免费额度用尽时会自动切到倍率最低的付费模型）")

    if mode == "lite":
        # lite 档到此为止：剩下的都是「锦上添花」，不值那几百 token 的钱。
        return "\n".join(lines)

    # ---- full 档 ----
    accounts = data.get("accounts") or []
    if accounts:
        parts = []
        for a in accounts:
            bal = a.get("balance")
            bal_text = f"{bal} 积分" if bal is not None else "未知"
            exp = _fmt_ts(a.get("expires_at"))
            parts.append(f"{a['label']}（余额 {bal_text}，凭据 {exp} 到期）")
        lines.append("当前可用账号：" + "；".join(parts))

    tiers = [t for t in (data.get("tiers") or []) if not t.get("disabled")][:8]
    if tiers:
        seg = []
        for t in tiers:
            fu = int(t.get("free_upto") or 0)
            if t.get("flat_credit") is not None:
                seg.append(f"{t['model']} 按次 {t['flat_credit']}")
            elif fu > 0:
                seg.append(f"{t['model']} ≤{fu}token 免费")
            elif t.get("measured_credit") is not None:
                seg.append(f"{t['model']} 实测 {t['measured_credit']}")
            elif t.get("factor") is not None:
                seg.append(f"{t['model']} x{t['factor']}")
            else:
                seg.append(f"{t['model']} 未知")
        lines.append("倍率档位（从低到高）：" + " · ".join(seg))

    disabled = data.get("disabled_models") or []
    if disabled:
        lines.append("已停用（不要推荐给用户）：" + "、".join(disabled))

    lines.append(
        "如果用户问「你是什么模型」，回答要包含上面的模型 id（例如 "
        f"{active}），不要只说模型家族名。"
    )
    return "\n".join(lines)


def facts_text(model: str | None = None, mode: str = "full",
               est_tokens: int | None = None) -> str:
    return render(collect(), model, mode=mode, est_tokens=est_tokens)


def pick_mode(payload: dict[str, Any]) -> str:
    """这次请求用哪档事实块。

    规则（路由之前就能定，不产生循环依赖）：
    - 事实注入关掉 → "off"
    - 用户问到了模型/倍率/额度/账号 → "full"（那时候这些信息才有用）
    - 否则 → "lite"（省下的额度比多写几行明细值钱）
    """
    if not enabled():
        return "off"
    msgs = payload.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return "off"
    for m in msgs:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if not isinstance(content, str):
            continue
        if SENTINEL in content:
            return "off"      # 已经插过了，不再重复插
        low = content.lower()
        for kw in _ASK_KEYWORDS:
            if kw in low:
                return "full"
    return "lite"


def token_estimate(mode: str) -> int:
    """该档事实块大概多少 token（用于路由前的总 token 估算）。"""
    return int(MODE_TOKEN_EST.get(mode, 0))


def inject(
    payload: dict[str, Any],
    model: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """往 payload 的 messages 里插一条事实块。

    插在**第一条 user 之前**，不改客户端自己的 system（不动别人的提示词）。
    已经插过就不重复插。
    """
    if mode is None:
        mode = pick_mode(payload)
    if mode == "off":
        return payload
    msgs = payload.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return payload
    for m in msgs:
        if isinstance(m, dict) and isinstance(m.get("content"), str) and SENTINEL in m["content"]:
            return payload
    block = {"role": "system", "content": facts_text(model, mode=mode)}
    # 插在**第一条非 system 消息之前**：既不动客户端自己的 system，
    # 也不会把事实块甩到 tool 结果后面（Responses 的工具续接场景）。
    idx = next(
        (i for i, m in enumerate(msgs)
         if isinstance(m, dict) and m.get("role") != "system"),
        len(msgs),
    )
    new_msgs = list(msgs)
    new_msgs.insert(idx, block)
    out = dict(payload)
    out["messages"] = new_msgs
    return out


def status_payload() -> dict[str, Any]:
    """`GET /v1/gateway/status` 的返回体。"""
    data = collect()
    return {
        "public_model": data.get("public_model"),
        "active_model": data.get("active_model"),
        "manual_model": data.get("manual_model"),
        "strategy": data.get("strategy"),
        "free_first": data.get("free_first"),
        "allow_paid": data.get("allow_paid"),
        "disabled_models": data.get("disabled_models"),
        "model_order": data.get("model_order"),
        "accounts": data.get("accounts"),
        "free_models": data.get("free_models"),
        "tiers": data.get("tiers"),
        "cooldowns": data.get("cooldowns"),
        "built_at": data.get("built_at"),
    }
