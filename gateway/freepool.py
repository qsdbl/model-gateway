"""免费池权威口径（R1.5，2026-11-21）：只认官方标记的免费模型。

为什么需要这个模块（用户拍板，红线）：
----------------------------------------
此前「免费」有两个互相打架的口径：
- 调度器 `_tier` 用「本次请求 est_cost<=0」——付费模型在极短输入下也扣 0，
  于是 space-bunny/hunyuan-chat 这种 0.08x 付费模型被当成「免费档」优先用；
- 健康度/流向图用 costmodel.robust_free（实测推断），又和上面不一致。

用户明确要求：**两边绝对统一，只认官方目前标记为免费模型的才算免费**，
否则可能导致额外积分扣减。同时拍板：
- **免费池身份**（本模块）：目录 pricing=free / 倍率 0 的模型，才属于免费池。
  用于健康度条、流向图过滤、FREE 角标、免费池统计。
- **本次扣费**（est_cost<=0）：仅用于调度排序优先级（极短请求省钱），
  但**不贴「免费」标签**——这是两个不同的概念，绝不混用。

两个平台的官方标记情况（实测 11-21）：
- WorkBuddy：目录 `pricing=free` / `credits_factor=0`；线上只有 hy4flash-free。
- Qoder：目录是硬编码表，`is_free=None`（官方不标价、费率未标定），
  所以按本口径**没有可识别的免费模型**。这是诚实的，不能靠名字带 free 就猜。

本模块是免费池身份的**唯一真源**；forecast / store.switch_history / admin 都从这里取。
"""
from __future__ import annotations

from typing import Any


def official_free_models() -> dict[str, dict[str, Any]]:
    """官方标记免费的全部模型：{model_name: {display_name, credits_factor, platform}}。

    只信两平台 catalog 里的官方字段（pricing=free / credits_factor=0）。
    Qoder 目录不标免费 → 不返回它的模型。取不到目录时宁可返回空（fail-safe：
    没证据就当没有免费模型，绝不靠猜测把付费模型塞进免费池）。
    """
    out: dict[str, dict[str, Any]] = {}
    # WorkBuddy：pricing=free 或倍率 0 才算
    try:
        from .workbuddy_upstream import catalog as wb_catalog

        for m in wb_catalog() or []:
            mid = m.get("model_name") or ""
            if not mid:
                continue
            factor = m.get("credits_factor")
            if m.get("pricing") == "free" or factor == 0:
                out[mid] = {
                    "display_name": m.get("display_name") or mid,
                    "credits_factor": factor,
                    "platform": "workbuddy",
                }
    except Exception:
        pass
    # Qoder：官方不标免费，目录 is_free 恒 None —— 按红线不纳入。
    # 保留这段是为了将来官方一旦提供标记，只需在这里补判据，不用改调用方。
    try:
        from .qoder_upstream import catalog as qd_catalog

        for m in qd_catalog() or []:
            mid = m.get("model_name") or ""
            if not mid:
                continue
            factor = m.get("credits_factor")
            if m.get("pricing") == "free" or factor == 0:
                out[mid] = {
                    "display_name": m.get("display_name") or mid,
                    "credits_factor": factor,
                    "platform": "qoder",
                }
    except Exception:
        pass
    return out


def is_official_free(model_name: str | None) -> bool:
    """这个模型是否属于官方免费池。单一布尔判据，供各模块复用。"""
    if not model_name:
        return False
    return model_name in official_free_names()


def official_free_names() -> set[str]:
    """官方免费模型名集合（方便一次性取集合做过滤）。"""
    return set(official_free_models())


# 上一次检测到的免费模型集合（进程内，用于定时刷新时报告变化）
_last_free: set[str] | None = None


def refresh(force: bool = True) -> dict[str, Any]:
    """强制刷新两平台目录缓存，返回官方免费模型的当前集合与变化。

    用途：定时任务定期调用，检测官方上新/下架免费模型（例如 hy4flash-free
    之后换了新免费模型），并把变化记进调度日志。用户要的就是这个能力。

    `force=True` 时先清掉 WorkBuddy 的 catalog TTL 缓存，确保拿到的是上游最新
    目录而不是 60s 内的旧快照。Qoder 目录是硬编码表，无缓存，且官方不标免费。
    """
    global _last_free
    if force:
        try:
            from . import workbuddy_upstream as wb

            wb._CATALOG_CACHE["rows"] = None
            wb._CATALOG_CACHE["at"] = 0.0
        except Exception:
            pass
    now_free = official_free_names()
    added = sorted(now_free - (_last_free or set()))
    removed = sorted((_last_free or set()) - now_free)
    changed = bool(_last_free is not None and (added or removed))
    _last_free = set(now_free)
    return {
        "free_models": sorted(now_free),
        "added": added,
        "removed": removed,
        "changed": changed,
        "checked_at": __import__("time").time(),
    }
