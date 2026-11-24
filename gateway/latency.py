"""R2.17：候选的「近期慢请求」统计（只在内存中，不落库）。

用途：low 模式在同一档位内把最近明显变慢的 (账号, 模型) 往后排，
让下一个候选先试。**不跨档位**——绝不因为慢就自动切到付费模型。
（用户拍板：不做切付费。）

样本只取成功请求：取消请求的耗时受客户端耐心影响，不能当上游慢的证据；
而且记账 bug 修好前取消请求还会出现几万秒的假值。
"""
from __future__ import annotations

import statistics
import time

WINDOW_SECONDS = 10 * 60
MAX_SAMPLES = 20

# key = (model_name, account_label)，value = [(记录时刻, 耗时秒), ...]
_SAMPLES: dict[tuple[str, str], list[tuple[float, float]]] = {}


def note(model: str | None, account: str | None, seconds: float) -> None:
    if not model or not account or seconds <= 0:
        return
    now = time.monotonic()
    samples = _SAMPLES.setdefault((model, account), [])
    samples.append((now, float(seconds)))
    # 只留窗口内的最近若干条
    cutoff = now - WINDOW_SECONDS
    _SAMPLES[(model, account)] = [s for s in samples if s[0] >= cutoff][-MAX_SAMPLES:]


def is_slow(model: str | None, account: str | None,
            threshold_s: float = 90.0, min_samples: int = 2) -> bool:
    """近窗口内中位数超过阈值才算慢；样本不足如实返回 False。"""
    if not model or not account or threshold_s <= 0:
        return False
    samples = _SAMPLES.get((model, account)) or []
    if len(samples) < min_samples:
        return False
    return statistics.median(s for _, s in samples) >= threshold_s


def clear() -> None:
    """测试用：清空内存样本。"""
    _SAMPLES.clear()
