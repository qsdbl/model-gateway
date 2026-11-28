"""免费池健康度与耗尽预测（R1.1，2026-11-21）。

## 机制真相（先读这个，别按直觉做）

「免费」不是账号累计额度，而是两层（线上实测，别再搞混）：
1. **每请求长度档**：hy4flash-free 从 123 到 201165 token 全扣 0 —— 真免费模型
   没有额度池；hy4flash 等按本次输入 token 长度计费，免费=长度落在免费区间。
2. **上游时段配额（429 code 6004）**：免费模型也会撞「您的使用量已超出频率限制，
   将在 2026-11-19 21:45:41 UTC+8 重置」——msg 里带**固定重置时刻**（非滑动窗口，
   14 次触发重置时间一字不变）。这才是用户说的「免费额度用完了」。
   邮件原文建议「切换其他模型继续使用」。

所以预测的不是「额度还剩多少」（不存在这个量），而是：
- **当前免费池有多健康**（多少 账号×免费模型 可用 / 冷却中）；
- **按最近请求强度，预计多久后免费池被打穿**（全部冷却或全部付费）；
- **已知最近一次 6004 重置时刻**（过了它免费模型就回来了）。

数据全部现成，不加埋点：
- `requests`：请求时刻/模型/账号/错误原文（429 时刻与重置时刻）/prompt_tokens
- `cooldowns`：当前冷却中的 (账号,模型) 与到期时刻
- `costmodel` 价格曲线：判断「这个模型现在算不算免费」
- `scheduler._candidate_pool`：真实候选池（含账号停用/余额过滤）

预测是**估算**，V1 规则法：历史「打穿事件」的平均存活分钟数 ÷ 当前强度系数。
「打穿事件」= 连续 N 条请求都是付费模型（免费池耗尽的现场证据）。
样本不足时如实给「样本不足」，不编数。
"""
from __future__ import annotations

import re
import time
from typing import Any

# 「打穿」判定：最近这么多分钟内的请求全是付费档 → 免费池已耗尽的现场
BURN_WINDOW_MIN = 30
# 强度窗口：用最近这么长时间的请求数当「当前强度」
INTENSITY_WINDOW_MIN = 60
# 历史打穿事件最多回看天数
HISTORY_DAYS = 30


def _default_store():
    from .store import store
    return store


def _paid_ratio_recent(window_min: int, st=None) -> dict[str, Any]:
    """最近 N 分钟请求里付费档占比。public_model 只记 low/high/personal-agent，
    免费与否要看 model_name 的价格曲线，这里用 actual_credit>0 近似
    （免费模型 credit=0；付费模型绝大多数回传了金额）。"""
    st = st or _default_store()

    since = time.time() - window_min * 60
    with st._lock:
        row = st._conn.execute(
            """SELECT count(*) AS n,
                      sum(CASE WHEN coalesce(actual_credit,0) > 0 THEN 1 ELSE 0 END) AS paid,
                      sum(CASE WHEN model_name LIKE '%-free' THEN 1 ELSE 0 END) AS freem
               FROM requests WHERE created_at >= ?""",
            (since,),
        ).fetchone()
    n = int(row["n"] or 0)
    return {
        "requests": n,
        "paid_requests": int(row["paid"] or 0),
        "free_model_requests": int(row["freem"] or 0),
        "paid_ratio": (float(row["paid"]) / n) if n else None,
    }


def _catalog_free_models() -> dict[str, dict[str, Any]]:
    """官方标记免费的模型（pricing=free / 倍率 0）。

    R1.5：口径抽到 `gateway.freepool`（免费池唯一真源），本函数保留为薄包装，
    兼容现有测试对 `_catalog_free_models` 的 monkeypatch。
    """
    from .freepool import official_free_models

    return official_free_models()


def _real_length_percentile(st=None, days: int = 7, pct: int = 50) -> int | None:
    """近 N 天真实请求的 token 长度分位数（默认 P50 中位数）。

    健康度必须用**用户真实的输入长度**算，不能用 200 token 这种示例值：
    实测用户真实请求中位数 ~11 万 token，用 200 探针会把
    「仅短请求免扣」的模型误报成免费模型（11-21 的 bug 根因）。
    """
    st = st or _default_store()
    since = time.time() - days * 86400
    with st._lock:
        rows = st._conn.execute(
            "SELECT token_estimate FROM requests "
            "WHERE created_at >= ? AND token_estimate IS NOT NULL "
            "AND token_estimate > 0 ORDER BY token_estimate",
            (since,),
        ).fetchall()
    vals = [int(r["token_estimate"] or 0) for r in rows]
    if not vals:
        return None
    idx = min(len(vals) - 1, int(len(vals) * pct / 100))
    return vals[idx]


def _pool_health(est_tokens: int | None = None, st=None) -> dict[str, Any]:
    """当前免费池健康度：按真实候选池（含冷却/停用/余额过滤）统计。

    口径（R1.3，2026-11-21 用户三次纠正后定稿）：
    - **真免费**（目录 pricing=free）：hy4flash-free。无论多长都扣 0，
      这才是免费池的真正容量。
    - **短请求免扣**（costmodel free_upto > 0 但目录不是 free）：space-bunny
      (≤258 token)、hunyuan-chat (≤1243 token) 这类——它们是**付费模型**，
      只是极短输入恰好不扣钱，不能算进免费池。
    - **容量分母 = 真免费模型 × 当前可用账号数**，不是「全部候选」——
      拿 142 个候选当分母会把免费池算成 8%，看着像告急，其实满的。
    """
    from .scheduler import _candidate_pool

    st = st or _default_store()
    real_tokens = _real_length_percentile(st=st)
    # 优先用真实长度；没有历史样本才退回默认探针值。
    probe = est_tokens if est_tokens is not None else (real_tokens or 200)

    pool, _rejected = _candidate_pool(est_tokens=probe)
    # R2.49：固定容量分母——忽略临时冷却的池子，算「本来能有多少 账号×免费模型」。
    # 分子（free_pairs/free_accounts）仍用当前真实可用池，两者分开后
    # 账号冷却就会显示 1/2 而不是 1/1（旧的 1/1 掩盖了「少了一个账号」）。
    try:
        base_pool, _ = _candidate_pool(est_tokens=probe, ignore_cooldowns=True)
    except TypeError:  # 兼容 monkeypatch 掉参数的老测试 stub
        base_pool = pool
    total = len(pool)
    unknown = [c for c in pool if c.est_cost is None]
    catalog_free = _catalog_free_models()

    # --- 真免费候选：目录标 free，且此刻估算扣费为 0 ---
    truly_free = [c for c in pool
                  if c.model_name in catalog_free
                  and c.est_cost is not None and c.est_cost <= 0]
    # --- 短请求免扣候选：目录不标 free，但本次长度恰好免扣 ---
    short_free = [c for c in pool
                  if c.model_name not in catalog_free
                  and c.est_cost is not None and c.est_cost <= 0]

    def _by_model(items):
        d: dict[str, int] = {}
        for c in items:
            d[c.model_name] = d.get(c.model_name, 0) + 1
        return d

    truly_free_models = _by_model(truly_free)
    short_free_models = _by_model(short_free)

    # 防御（2026-11-21 抖动事故）：目录里有真免费模型，但候选里它的 est_cost
    # 全是 None = 价格曲线缓存被破坏（残缺缓存），不是免费池真空。
    # 此时如实报 unknown，不把「数据缺失」伪装成「免费池为空」。
    missing_free = sorted(
        mid for mid in catalog_free
        if mid not in truly_free_models
        and any(c.model_name == mid for c in pool)
        and all(c.est_cost is None for c in pool if c.model_name == mid)
    )

    # 当前可用账号（分子口径）
    free_accounts: dict[str, list[str]] = {}
    for c in truly_free:
        free_accounts.setdefault(c.account_label, []).append(c.model_name)
    # R2.49：容量分母 = 真免费模型数 × 「池子本来有的」账号数（忽略临时冷却）。
    # 基准池里所有带真免费模型的账号都算容量，哪怕它此刻正在冷却——
    # 这样 1 个账号冷却时就显示 1/2，用户一眼看出「少了一个」，而不是骗人的 1/1。
    base_free_accounts: set[str] = set()
    for c in base_pool:
        if c.model_name in catalog_free and c.est_cost is not None and c.est_cost <= 0:
            base_free_accounts.add(c.account_label)
    # 至少不能小于当前可用账号数（防 base_pool monkeypatch/异常时倒挂）
    capacity = len(truly_free_models) * max(len(base_free_accounts), len(free_accounts))

    return {
        "est_tokens": probe,
        "real_tokens_p50": real_tokens,
        "candidates": total,
        # —— 真免费池（进度条用这个）——
        "free_pairs": len(truly_free),              # 当前可用的真免费组合数
        "free_capacity": capacity,                 # 满池容量（真免费模型 × 账号）
        "free_models": sorted(truly_free_models),  # 真免费模型名
        "free_model_counts": truly_free_models,
        "free_accounts": {k: sorted(set(v)) for k, v in sorted(free_accounts.items())},
        # —— 短请求免扣（另列，不算免费池）——
        "short_free_pairs": len(short_free),
        "short_free_models": sorted(short_free_models),
        "short_free_model_counts": short_free_models,
        "short_free_limits": _short_free_limits(sorted(short_free_models)),
        # —— 其余 ——
        "unknown_pairs": len(unknown),
        "paid_pairs": total - len(truly_free) - len(short_free) - len(unknown),
        # 目录标 free 但此刻价格未知（缓存缺失等）；非空=数据异常，不能当免费池为空
        "free_pricing_missing": missing_free,
    }


def _short_free_limits(model_names: list[str]) -> dict[str, int]:
    """短请求免扣模型的 token 上限（free_upto）。"""
    out: dict[str, int] = {}
    try:
        from . import costmodel

        for m in model_names:
            try:
                out[m] = int(costmodel.curve(m).free_upto or 0)
            except Exception:
                continue
    except Exception:
        pass
    return out


def _burn_events(st=None) -> list[dict[str, Any]]:
    """历史「打穿事件」：连续付费请求的起始时刻与持续分钟数。

    算法：按时间扫当天请求，若连续 >= BURN_WINDOW_MIN 分钟全是付费档
    （actual_credit>0），记一次事件。事件开始 = 第一条付费请求，
    事件结束 = 之后第一条免费请求（或当日最后一条）。
    样本少是常态（用户偶尔高强度），如实返回。
    """
    st = st or _default_store()

    since = time.time() - HISTORY_DAYS * 86400
    with st._lock:
        rows = st._conn.execute(
            """SELECT created_at, coalesce(actual_credit,0) AS credit
               FROM requests WHERE created_at >= ? ORDER BY created_at""",
            (since,),
        ).fetchall()
    events: list[dict[str, Any]] = []
    run_start: float | None = None
    run_last: float | None = None
    for r in rows:
        ts = float(r["created_at"])
        paid = float(r["credit"] or 0) > 0
        if paid:
            if run_start is None:
                run_start = ts
            run_last = ts
        else:
            if run_start is not None and (run_last - run_start) >= BURN_WINDOW_MIN * 60:
                events.append({"start": run_start, "end": run_last,
                               "minutes": round((run_last - run_start) / 60, 1)})
            run_start = None
            run_last = None
    if run_start is not None and (run_last - run_start) >= BURN_WINDOW_MIN * 60:
        events.append({"start": run_start, "end": run_last,
                       "minutes": round((run_last - run_start) / 60, 1)})
    return events


def _last_429_reset(st=None) -> dict[str, Any]:
    """最近一次 429 code 6004 的时刻与上游给的重置时刻。"""
    st = st or _default_store()
    from .probe import parse_reset_time

    with st._lock:
        row = st._conn.execute(
            """SELECT created_at, account_label, model_name, error
               FROM requests WHERE error LIKE '%6004%' ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
    if not row:
        return {"seen": False}
    reset = parse_reset_time(row["error"] or "")
    return {
        "seen": True,
        "at": float(row["created_at"]),
        "account": row["account_label"],
        "model": row["model_name"],
        "reset_at": reset,
        "reset_pending": bool(reset and reset > time.time()),
    }


def forecast(st=None) -> dict[str, Any]:
    """汇总：健康度 + 强度 + 预测。只做估算，样本不足如实说。"""
    st = st or _default_store()
    health = _pool_health()
    intensity = _paid_ratio_recent(INTENSITY_WINDOW_MIN, st)
    burns = _burn_events(st)
    last429 = _last_429_reset(st)

    # ---- 预测（V1 规则法）----
    # 历史平均存活分钟数 = 打穿事件平均时长；强度系数 = 最近 1 小时请求数
    # ÷ 历史事件期间的典型强度。样本 <2 就不预测，只给健康度。
    estimate: dict[str, Any] = {"available": False, "reason": ""}
    if health["free_pairs"] == 0:
        estimate = {"available": True, "minutes_left": 0,
                    "reason": "免费池当前为空（全部冷却/付费），已经在付费模式"}
    elif len(burns) >= 2:
        avg_min = sum(b["minutes"] for b in burns) / len(burns)
        # 事件期间典型强度：粗略用事件期间请求密度。这里简化为当前强度占比：
        # 当前强度越高，耗尽越快 —— minutes_left ≈ avg_min × (典型强度/当前强度)
        n_now = intensity["requests"] or 0
        # 无请求时给保守长估计
        if n_now <= 0:
            estimate = {"available": True, "minutes_left": None,
                        "reason": "最近 %d 分钟没有请求；历史平均免费池可撑 %.0f 分钟（高强度）"
                                  % (INTENSITY_WINDOW_MIN, avg_min)}
        else:
            # 强度归一：事件平均时长本身就是「那次强度下」的存活时间，
            # 直接给区间而非伪精确值。
            lo = max(1.0, avg_min * 0.6)
            hi = avg_min * 1.8
            estimate = {"available": True, "minutes_left": round(avg_min, 0),
                        "range": [round(lo), round(hi)],
                        "reason": "基于 %d 次历史打穿事件，平均 %.0f 分钟；当前强度 %d 次/小时"
                                  % (len(burns), avg_min, n_now)}
    else:
        estimate = {"available": False, "reason": "样本不足，暂不预测"}

    return {
        "health": health,
        "intensity": {**intensity, "window_min": INTENSITY_WINDOW_MIN},
        "burn_events": [
            {"start": b["start"], "end": b["end"], "minutes": b["minutes"]}
            for b in burns[-10:]                     # 最近 10 次够看趋势
        ],
        "burn_events_total": len(burns),
        "last_429": last429,
        "estimate": estimate,
        "generated_at": time.time(),
    }
