"""价格/余额查询与缓存（P5）。

规则（README P5，来自调研报告）：
- 缓存键含：账号、地区、产品、活动、时间、更新时间 —— 避免不同场景串味。
- 价格查询失败=未知（None），不能算免费。
- 优惠过期失效，不能继续按缓存的免费标签调度。
- 免费标记与价格字段冲突 → 暂停该渠道免费资格。
- 无合格免费模型 → 明确提示，不偷偷换收费模型。
- 实际扣费只展示可取得的真实值；取不到=未知，绝不用估算冒充账单。

查询函数可注入（P7 用真实平台查询替换）；本阶段用 mock 验证缓存与过期逻辑。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from .router import Router


@dataclass
class PriceSnapshot:
    """一次价格/余额查询快照。"""
    is_free: bool | None          # None=未知（查询失败也是未知）
    price_factor: float | None
    promotion_until: float | None
    balance: float | None
    balance_unit: str | None
    fetched_at: float
    region: str = "default"
    product: str = "default"
    campaign: str = "default"
    available: bool = True        # False=查询失败

    def is_free_now(self, now: float | None = None) -> bool | None:
        """带时效的免费判定（与 Router 一致的规则）。"""
        now = now if now is not None else time.time()
        if not self.available or self.is_free is None:
            return None
        if self.promotion_until is not None and self.promotion_until < now:
            return False
        if self.is_free and self.price_factor is not None and self.price_factor > 1:
            if self.promotion_until is None or self.promotion_until < now:
                return None  # 冲突→暂停免费资格
        return bool(self.is_free)

    def is_expired(self, ttl: float, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        effective_ttl = getattr(self, "_ttl_override", ttl)
        return (now - self.fetched_at) > effective_ttl


# 查询函数签名：(account_label, platform, model_name) -> PriceSnapshot
PriceFetcher = Callable[[str, str, str], PriceSnapshot]


class PriceCache:
    """带上下文键的 TTL 缓存。失败不缓存为免费；过期强制重查。"""

    def __init__(self, fetcher: PriceFetcher, ttl: float = 300.0) -> None:
        self.fetcher = fetcher
        self.ttl = ttl
        self._store: dict[tuple[str, str, str, str, str, str], PriceSnapshot] = {}

    @staticmethod
    def make_key(
        account_label: str, platform: str, model_name: str,
        region: str = "default", product: str = "default", campaign: str = "default",
    ) -> tuple[str, str, str, str, str, str]:
        """缓存键含账号/平台/模型/地区/产品/活动 —— 加更新时间由 value 的 fetched_at 承担。"""
        return (account_label, platform, model_name, region, product, campaign)

    def get(
        self, account_label: str, platform: str, model_name: str,
        *, region: str = "default", product: str = "default", campaign: str = "default",
        now: float | None = None,
    ) -> PriceSnapshot:
        now = now if now is not None else time.time()
        key = self.make_key(account_label, platform, model_name, region, product, campaign)
        cached = self._store.get(key)
        if cached is not None and not cached.is_expired(self.ttl, now):
            return cached
        # 过期或无缓存 → 查询；查询失败也存（available=False），但绝不会被判免费
        snap = self.fetcher(account_label, platform, model_name)
        # 失败快照用短 TTL（30s）缓存，避免长时间卡在未知且不过度打扰上游。
        # 用 _fail_ttl 标记让 is_expired 按短 TTL 判断，不篡改 fetched_at 语义。
        if not snap.available:
            snap._ttl_override = 30.0
        self._store[key] = snap
        return snap

    def invalidate(self, account_label: str, platform: str, model_name: str, **kw: str) -> None:
        key = self.make_key(
            account_label, platform, model_name,
            kw.get("region", "default"), kw.get("product", "default"),
            kw.get("campaign", "default"),
        )
        self._store.pop(key, None)

    def free_decision(
        self, account_label: str, platform: str, model_name: str,
        **kw: str,
    ) -> tuple[bool | None, str]:
        """返回 (是否免费, 说明)。用于后台模型页展示与路由前置判断。"""
        snap = self.get(account_label, platform, model_name, **kw)
        free = snap.is_free_now()
        if not snap.available:
            return None, "价格查询失败 → 未知，不计免费"
        if free is True:
            if snap.promotion_until:
                return True, f"免费（优惠至 {snap.promotion_until:.0f}）"
            return True, "免费"
        if free is False:
            if snap.promotion_until and snap.promotion_until < time.time():
                return False, "优惠已过期 → 不再免费"
            return False, "收费"
        # free is None
        if snap.is_free and snap.price_factor and snap.price_factor > 1:
            return None, "免费标记与倍率冲突 → 暂停免费资格"
        return None, "价格未知 → 不计免费"


def no_qualified_free_message() -> str:
    """无合格免费模型时的明确提示（不偷换收费模型）。"""
    return (
        "没有合格的免费模型可用。已停止请求，未自动切换到收费模型。"
        "如需允许收费兜底，请显式开启 allow_paid。"
    )
