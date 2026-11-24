"""计费模型（P41）——上游是「按输入 token 计费」，不是「免费/付费」二分。

**这一版推翻了 P34–P37 的计费模型。** 2026-11-19 线上实测（hy4flash 账号，
32 个 chat 模型 × 5 档长度，共 160 次真实请求）：

| 模型 | 114tok | 414tok | 1214tok | 2514tok | 5014tok |
|---|---|---|---|---|---|
| **hy4flash-free** | 0 | 0 | 0 | 0 | **0** ← 真免费（30023 token 仍是 0） |
| **deepseek-v4.1-flash** | 0.01 | 0.01 | 0.01 | 0.01 | 0.01 ← 按次封顶 |
| **hunyuan-chat** | 0 | 0 | 0.01 | 0.01 | 0.02 |
| **hy4flash** | 0 | 0.01 | 0.02 | 0.04 | 0.08 |
| **hy4std** | 0.03 | 0.07 | 0.2 | 0.41 | 0.82 |
| **kimi-k3-1** | 0.15 | 0.31 | 0.62 | 0.96 | 1.57 |

三条硬结论：

1. **免费额度是「每请求约 110–460 token」的输入长度门槛，不是模型属性。**
   同一模型，短请求免费、长请求收费。旧的「免费池/付费池」二分因此是错的维度——
   所有模型超过门槛都要收费，「免费优先」实际等于「短请求优先」。

2. **收费随长度近似线性**（hy4flash：423→0.01、1214→0.02、2514→0.04、5023→0.08），
   金额按 0.01 步长向上取整。

3. **目录里的 `credits_factor` 与实际费率不成比例**：hy4std / balanced-model /
   deepseek-v4.1-flash 三个目录倍率不同，实际收费曲线几乎一样；而 kimi-k3-1
   的倍率只比 hy4flash 高一点点，实际贵 20 倍。**不能拿目录倍率排序。**

所以本模块不拟合固定公式，而是**存观测点、做分段线性插值 + 外推**：
观测点来自真实请求（零额外成本）和探针（冷启动/校正），随时可用新数据校准。
这比硬编码阈值稳，也比硬编码公式准。

用法：
    from . import costmodel
    costmodel.observe("hy4flash", tokens=423, credit=0.01)   # 记一笔观测
    costmodel.cost_at("hy4flash", 800)                        # → 0.01（估算）
    costmodel.ranking(800)                                    # → [(模型, 预计扣费), ...]
"""
from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable

# 金额最小步长：上游的 credit 都是 0.01 的整数倍
CREDIT_STEP = 0.01

# 每个模型最多保留多少个观测点（超出后按 tokens 均匀抽稀，保留边界）
MAX_POINTS = 48

# 曲线缓存的最长有效期（秒）。库里存的是观测点不是拟合结果，
# 所以「过期」只影响缓存新鲜度，不影响精度。
CACHE_TTL = 30.0

# 真免费模型的「噪声免疫」阈值（2026-11-20 线上误发提醒邮件后加）。
#
# 事故链：hy4flash-free 实测从 123 到 201165 token 全部扣 0，是真免费模型。
# 但曲线里混进一条上游异常扣费（id160：token=2 却扣 0.01），always_free
# 立刻从 True 翻成 False，free_upto 从 201165 崩到 179。调度器把一个真免费
# 模型按长度外推成 76.49 积分 → 误选付费 hy4flash（2.61）→ 误发
# 「[网关] low 开始消耗积分」邮件。加上曲线缓存 30s + 抽稀，噪声点被随机
# 挤进/挤出，免费模型在「免费/天价」间反复横跳。
#
# 判据：免费观测覆盖极长（远超任何「短请求免费」档的几百 token）+
# 收费点稀少且都是最小计费步长的小额 → 判定为真免费，孤立误扣当噪声忽略。
ROBUST_FREE_MIN_SPAN = 20_000       # 免费点最大 token ≥ 2 万：不可能是短请求免费档
ROBUST_FREE_MAX_PAID = 2            # 允许的孤立异常收费点绝对个数下限
ROBUST_FREE_PAID_RATIO = 20         # 或免费点数的 1/20（取两者较大）
ROBUST_FREE_MAX_NOISE_CREDIT = 0.01  # 噪声扣费上限 = 上游最小计费步长


@dataclass(frozen=True)
class PricePoint:
    """一个观测点：某次请求用了多少输入 token，实际扣了多少积分。"""

    tokens: int
    credit: float

    def as_list(self) -> list[float]:
        return [float(self.tokens), float(self.credit)]


@dataclass
class PriceCurve:
    """一个模型的价格曲线（由观测点拟合而成）。"""

    model_name: str
    points: tuple[PricePoint, ...] = ()

    # ---------- 派生属性 ----------

    def _split(self) -> tuple[list[PricePoint], list[PricePoint]]:
        """把观测点拆成「可信的免费前缀」和「收费点」。

        **为什么要拆**（线上实测踩到的坑）：单次观测会偶发「免费」。
        2026-11-19 标定 hy4flash 时，1223 token 那次扣了 0.00，而重复测 4 次
        稳定是 0.02 —— 是一次偶发。若直接把所有免费点都当成免费区，
        free_upto 会被撑到 1223，调度器就会把 1200 token 的请求误判成免费，
        白白用掉付费模型。

        所以规则是：**只有从最小观测点开始连续为 0 的那一段才算免费区**，
        一旦出现收费点，之后再出现的 0 都视为抖动噪声，从曲线里剔除。
        """
        pts = sorted(self.points, key=lambda p: p.tokens)
        first_paid: int | None = None
        for i, p in enumerate(pts):
            if p.credit > 0:
                first_paid = i
                break
        if first_paid is None:
            return pts, []                      # 全都免费 → 真免费
        free_part = pts[:first_paid]
        paid_part = [p for p in pts[first_paid:] if p.credit > 0]
        return free_part, paid_part

    @property
    def free_upto(self) -> int:
        """已知的免费上限：**连续免费前缀**的最长 token 数。

        没有观测到免费点就是 0（= 没有已知免费区）。
        抗噪：真免费模型（robust_free）即便混进个别误扣点，免费上限也取
        全部免费观测的最大值，不因孤立噪声从 20 万崩到 179。
        """
        if self.robust_free:
            return max((p.tokens for p in self.points if p.credit <= 0), default=0)
        free_part, _ = self._split()
        return max((p.tokens for p in free_part), default=0)

    @property
    def flat_credit(self) -> float | None:
        """按次计费模型的固定价：所有观测点 credit 相同且非 0。

        deepseek-v4.1-flash 就是这种：114 token 和 5014 token 都扣 0.01。
        """
        if len(self.points) < 2:
            return None
        credits = {round(p.credit, 6) for p in self.points}
        if len(credits) == 1:
            only = credits.pop()
            return only if only > 0 else None
        return None

    @property
    def measured_max_tokens(self) -> int:
        return max((p.tokens for p in self.points), default=0)

    @property
    def measured_min_credit(self) -> float | None:
        """观测到的最低扣费（拿它当「这个模型至少要花多少」的参考）。"""
        vals = [p.credit for p in self.points]
        return min(vals) if vals else None

    @property
    def always_free(self) -> bool:
        """真免费：所有观测点的扣费都是 0，且至少有 2 个观测点。

        实测 hy4flash-free 在 5023 token（更早测到 30023）时仍然扣 0 ——
        它不是「一段长度内免费」，而是**不限长度都免费**（至少在观测范围内）。
        这类模型要单独认出来，否则会被「按长度线性外推」误判成要花钱，
        白白让开免费额度。

        要 ≥2 个点：单点可能是偶发免费，不能据此断定「不限长度都免费」。
        """
        return len(self.points) >= 2 and all(p.credit <= 0 for p in self.points)

    @property
    def robust_free(self) -> bool:
        """抗噪版真免费判定：极长的免费观测 + 极少的小额误扣 → 仍算真免费。

        和 `always_free` 的区别：`always_free` 要求**一个收费点都没有**，
        一条上游异常扣费（极小 token 却扣 0.01）就能把它打回付费档，进而让
        free_upto 崩塌、把真免费模型外推成天价（2026-11-20 误发提醒邮件的事故）。
        `robust_free` 用三条同时满足的硬条件圈定「铁定真免费」的模型：

        1. 免费观测的最长 token ≥ ROBUST_FREE_MIN_SPAN（2 万）——
           「短请求免费档」最多几百 token，覆盖到 2 万说明它不长收费；
        2. 收费点极少（≤ ROBUST_FREE_MAX_PAID 个，或 ≤ 免费点数的 1/20）——
           偶尔一两次误扣是上游抖动，不是稳定费率；
        3. 每个收费点的扣费都 ≤ ROBUST_FREE_MAX_NOISE_CREDIT（0.01）——
           真付费模型这么长的请求会扣几积分，不该只有 1 分钱。

        三条缺一不可，避免把「少数长请求恰好免费」的付费模型误判成真免费。
        """
        if not self.points:
            return False
        free_pts = [p for p in self.points if p.credit <= 0]
        paid_pts = [p for p in self.points if p.credit > 0]
        if len(free_pts) < 2:
            return False
        if not free_pts or max(p.tokens for p in free_pts) < ROBUST_FREE_MIN_SPAN:
            return False
        allowed_paid = max(
            ROBUST_FREE_MAX_PAID,
            len(free_pts) // ROBUST_FREE_PAID_RATIO,
        )
        if len(paid_pts) > allowed_paid:
            return False
        return all(p.credit <= ROBUST_FREE_MAX_NOISE_CREDIT for p in paid_pts)

    # ---------- 估算 ----------

    def cost_at(self, tokens: int) -> float | None:
        """这次请求（输入 tokens）大概扣多少积分。**不知道就返回 None，不猜。**"""
        if not self.points:
            return None

        # 真免费模型（实测所有长度都扣 0）：不论多长都是 0，不做外推。
        # robust_free 额外容忍极少数上游异常误扣，避免单个噪声点把它打成天价。
        if self.always_free or self.robust_free:
            return 0.0

        flat = self.flat_credit
        if flat is not None:
            return flat

        free_part, paid_part = self._split()
        pts = free_part + paid_part          # 已剔除「抖动出来的免费点」
        if not pts:
            return None

        # 免费区：连续免费前缀的最长长度，再长就必然收费
        free_upto = max((p.tokens for p in free_part), default=0)
        if free_upto and tokens <= free_upto:
            return 0.0

        # 落在两个观测点之间 → 线性插值
        for lo, hi in zip(pts, pts[1:]):
            if lo.tokens <= tokens <= hi.tokens:
                if hi.tokens == lo.tokens:
                    return _round_up(max(lo.credit, hi.credit))
                ratio = (tokens - lo.tokens) / (hi.tokens - lo.tokens)
                return _round_up(lo.credit + ratio * (hi.credit - lo.credit))

        # 超出最大观测点 → 用最后两个点外推（斜率来自最远的两点，噪声最小）
        if len(pts) >= 2 and tokens > pts[-1].tokens:
            lo, hi = pts[-2], pts[-1]
            slope = (hi.credit - lo.credit) / (hi.tokens - lo.tokens)
            est = hi.credit + slope * (tokens - hi.tokens)
            return _round_up(max(hi.credit, est))

        # 低于最小观测点 → 从 (0, 0) 线性插值到第一个点
        first = pts[0]
        if tokens < first.tokens:
            if first.tokens <= 0:
                return _round_up(first.credit)
            ratio = max(0.0, tokens) / first.tokens
            return _round_up(first.credit * ratio)

        return _round_up(first.credit)

    def summary(self) -> dict[str, Any]:
        """给后台看的曲线摘要。"""
        return {
            "model_name": self.model_name,
            "points": len(self.points),
            "free_upto": self.free_upto,
            "always_free": self.always_free,
            "robust_free": self.robust_free,
            "flat_credit": self.flat_credit,
            "measured_max_tokens": self.measured_max_tokens,
            "min_credit": min((p.credit for p in self.points), default=None),
            "max_credit": max((p.credit for p in self.points), default=None),
        }


def _round_up(value: float) -> float:
    """向上取整到 0.01 步长。

    宁可高估不可低估：估低了会漏计费，用户看到「免费」结果却被扣钱，
    那是最糟的错法。上游金额本身就是 0.01 的整数倍，所以这个取整不引入偏差。
    """
    if value <= 0:
        return 0.0
    return math.ceil(value / CREDIT_STEP - 1e-9) * CREDIT_STEP


def fit_curve(model_name: str, points: Iterable[Any]) -> PriceCurve:
    """把观测点拟合成价格曲线。

    观测点可以是 PricePoint，也可以是 [tokens, credit] 列表（库里存的格式）。
    """
    out: list[PricePoint] = []
    for p in points or []:
        if isinstance(p, PricePoint):
            out.append(p)
            continue
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            try:
                out.append(PricePoint(int(p[0]), float(p[1])))
            except (TypeError, ValueError):
                continue
        elif isinstance(p, dict):
            try:
                out.append(PricePoint(int(p["tokens"]), float(p["credit"])))
            except (KeyError, TypeError, ValueError):
                continue
    # 同一 token 数取最大 credit（保守：不低估成本）
    best: dict[int, float] = {}
    for p in out:
        best[p.tokens] = max(best.get(p.tokens, 0.0), p.credit)
    ordered = tuple(PricePoint(t, best[t]) for t in sorted(best))
    return PriceCurve(model_name=model_name, points=ordered)


def merge_points(
    existing: Iterable[Any], new: Iterable[Any], limit: int = MAX_POINTS
) -> list[list[float]]:
    """合并观测点并抽稀，返回 [[tokens, credit], ...]（tokens 升序）。"""
    curve = fit_curve("", list(existing) + list(new))
    pts = list(curve.points)
    if len(pts) <= limit:
        return [p.as_list() for p in pts]
    # 均匀抽稀：永远保留首尾
    step = (len(pts) - 1) / (limit - 1)
    keep = {0, len(pts) - 1}
    for i in range(limit - 2):
        keep.add(int(round((i + 1) * step)))
    return [pts[i].as_list() for i in sorted(keep)]


# --------------------------------------------------------------------------
# 全局曲线册（缓存 + 落库）
# --------------------------------------------------------------------------

_lock = threading.RLock()
_cache: dict[str, PriceCurve] = {}
_cache_at = 0.0


def _store():
    from .store import store

    return store


def reload() -> None:
    """清缓存，下次访问重新读库。"""
    global _cache_at
    with _lock:
        _cache.clear()
        _cache_at = 0.0


def _all_curves_uncached() -> dict[str, PriceCurve]:
    try:
        raw = _store().get_pricing_points()
    except Exception:
        raw = {}
    return {mid: fit_curve(mid, pts) for mid, pts in (raw or {}).items()}


def all_curves() -> dict[str, PriceCurve]:
    """全部模型的价格曲线（带 30 秒缓存）。"""
    global _cache_at
    now = time.time()
    with _lock:
        if _cache and now - _cache_at < CACHE_TTL:
            return dict(_cache)
        curves = _all_curves_uncached()
        _cache.clear()
        _cache.update(curves)
        _cache_at = now
        return dict(curves)


def curve(model_name: str) -> PriceCurve:
    """单个模型的曲线。没有观测数据就是空曲线（cost_at 返回 None）。"""
    return all_curves().get(model_name, PriceCurve(model_name=model_name, points=()))


def cost_at(model_name: str, tokens: int) -> float | None:
    """这个模型跑这个长度的请求大概扣多少。未知返回 None。"""
    try:
        return curve(model_name).cost_at(tokens)
    except Exception:
        return None


def observe(model_name: str, tokens: int, credit: float | None) -> bool:
    """记一笔观测。返回是否写入了。

    `tokens` 要是**真实**的 prompt_tokens（上游 usage 给的），不是估算值——
    估算值会把自己的误差固化进曲线里，越用越偏。
    """
    if not model_name or credit is None:
        return False
    try:
        tokens = int(tokens)
        credit = float(credit)
    except (TypeError, ValueError):
        return False
    if tokens <= 0:
        return False
    try:
        stored = _store().merge_pricing_points(model_name, [[tokens, credit]])
    except Exception:
        return False
    # 写库后本进程缓存里的曲线过期了。
    # ⚠️ 不能只 _cache.pop(model_name)：那样会留下「缺了这个模型」的残缺缓存，
    # 而 _cache_at 时间戳不变 —— all_curves() 在剩余 30s 缓存期内直接命中它，
    # 导致该模型 est_cost=None。健康度会把真免费模型 hy4flash-free 误判成
    # unknown，免费池一会儿满一会儿空（2026-11-21 抖动事故根因）。
    # 正确做法：用刚写入合并后的完整点集立即重建该模型曲线放回缓存。
    # 其它模型不动，缓存保持完整且最新，时间戳无需改动。
    with _lock:
        if stored:
            _cache[model_name] = fit_curve(model_name, stored)
        else:
            _cache.pop(model_name, None)
    return bool(stored)


def ranking(tokens: int, candidates: Iterable[str] | None = None) -> list[tuple[str, float]]:
    """按「跑这个长度的预计扣费」给模型排序（便宜 → 贵）。

    没有曲线的模型排在最后（保守：不知道价格的东西不先上）。
    """
    curves = all_curves()
    pool = list(candidates) if candidates is not None else list(curves)
    known: list[tuple[str, float]] = []
    unknown: list[str] = []
    for mid in pool:
        c = curves.get(mid)
        cost = c.cost_at(tokens) if c else None
        if cost is None:
            unknown.append(mid)
        else:
            known.append((mid, cost))
    known.sort(key=lambda kv: (kv[1], kv[0]))
    return known + [(m, float("inf")) for m in sorted(unknown)]


def cheapest(
    tokens: int,
    candidates: Iterable[str] | None = None,
    exclude: set[str] | None = None,
) -> tuple[str | None, float | None]:
    """在候选模型里找「跑这个长度最便宜」的。

    返回 (模型名, 预计扣费)。没有可用曲线时返回 (None, None)——
    宁可说「不知道」，也不给一个拍脑袋的数。
    """
    exclude = exclude or set()
    for mid, cost in ranking(tokens, candidates):
        if mid in exclude:
            continue
        if cost == float("inf"):
            continue
        return mid, cost
    return None, None


def export_points() -> str:
    """导出全部观测点（调试用）。"""
    return json.dumps(
        {mid: [p.as_list() for p in c.points] for mid, c in all_curves().items()},
        ensure_ascii=False,
        indent=1,
    )
