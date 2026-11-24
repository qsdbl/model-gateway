"""扣费解析（P30）。

上游（WorkBuddy / copilot.tencent.com）在响应里带 `usage.credit` 表示这次真实扣了多少积分。
这是唯一可信的「花了多少钱」来源（P10 定的红线：拿不到就记 NULL，不许用 token 估算冒充）。

两种形态都要能取：
- 非流式：整只响应体 `{"usage": {"credit": 0.02, ...}}`
- 流式：最后一帧 `data: {"choices":[...], "usage": {"credit": 0.02, ...}}`

原来这段逻辑只在 `workbuddy_upstream` 里，且流式路径压根没接，导致 Chatbox / Codex /
手机 App（全是 stream=true）100% 记不到金额。抽出来给两个入口共用。
"""
from __future__ import annotations

import json
from typing import Any


def credit_from_usage(usage: Any) -> float | None:
    """从 usage 里取 credit；没有/不是数字就返回 None（未知，不猜）。"""
    if not isinstance(usage, dict):
        return None
    value = usage.get("credit")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def credit_from_sse_chunk(chunk: str) -> float | None:
    """从一帧 SSE 文本里取 `usage.credit`。

    先按字面量粗筛再解析：一场对话几十上百帧，逐帧 json.loads 没必要；
    但粗筛必须落在 `"usage"` 这个键上，避免正文里出现 "usage" 字样时误判。
    """
    text = chunk.strip()
    if not text.startswith("data:") or '"usage"' not in text:
        return None
    body = text[5:].strip()
    if not body or body == "[DONE]":
        return None
    try:
        obj = json.loads(body)
    except Exception:  # noqa: BLE001  半帧/坏帧一律当没有
        return None
    if not isinstance(obj, dict):
        return None
    return credit_from_usage(obj.get("usage"))


def prompt_tokens_from_usage(usage: Any) -> int | None:
    """从 usage 里取**真实**输入 token 数。

    P41：这个值是价格曲线的数据源——上游按输入 token 计费，只有真实
    prompt_tokens 才能把 (tokens, credit) 对上号。用估算值会把估算误差
    固化进曲线，越用越偏。
    """
    if not isinstance(usage, dict):
        return None
    for key in ("prompt_tokens", "input_tokens"):
        value = usage.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def prompt_tokens_from_sse_chunk(chunk: str) -> int | None:
    """从一帧 SSE 文本里取真实输入 token 数。"""
    text = chunk.strip()
    if not text.startswith("data:") or '"usage"' not in text:
        return None
    body = text[5:].strip()
    if not body or body == "[DONE]":
        return None
    try:
        obj = json.loads(body)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict):
        return None
    return prompt_tokens_from_usage(obj.get("usage"))
