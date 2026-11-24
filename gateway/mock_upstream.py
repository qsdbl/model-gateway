"""Mock 上游：P2/P3 用它验证协议与工具透传，不接真实平台。

按 README 第 5 节 P2/P3 要求，mock 需支持：
- 非流式 / SSE 流式；
- 工具调用（tool_calls）、多工具、参数分片；
- 断流 / 超时 / 错误等失败场景（用于验证不能记成成功）。

真实平台适配器在 P7 才用官方 SDK/自定义模型入口验证后替换。
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from .config import settings


# P30：测试可 monkeypatch 这个值，模拟「上游回传了 usage.credit」。
DEFAULT_CREDIT: float | None = None


class UpstreamError(Exception):
    """上游失败；reason 用于请求记录的错误分类。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _echo_text(payload: dict[str, Any]) -> str:
    msgs = payload.get("messages") or []
    last = ""
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            last = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
            break
    return f"[mock] 收到 {len(msgs)} 条消息：{last[:80]}"


def build_nonstream_response(payload: dict[str, Any], model: str | None = None) -> dict[str, Any]:
    """构造一个 OpenAI 兼容的非流式响应体。"""
    model = payload.get("model") or settings.public_model
    choice: dict[str, Any] = {
        "index": 0,
        "message": {"role": "assistant", "content": _echo_text(payload)},
        "finish_reason": "stop",
    }

    # 若请求带 tools，演示一个 tool_call（P3 测试工具往返用）
    if payload.get("tools"):
        first = payload["tools"][0]
        fn = first.get("function", {}) if isinstance(first, dict) else {}
        name = fn.get("name", "mock_tool")
        choice["message"] = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_mock_1",
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
            ],
        }
        choice["finish_reason"] = "tool_calls"

    usage: dict[str, Any] = {
        "prompt_tokens": 0,  # mock 不提供真实 token 账单
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    credit = _mock_usage_credit(payload)
    if credit is not None:
        usage["credit"] = credit

    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [choice],
        "usage": usage,
        # P3 透传验证：把收到的消息结构原样回显，供测试断言工具/图片未被剥离
        "_echo_messages": payload.get("messages", []),
        "_echo_tools": payload.get("tools", []),
    }


def _mock_usage_credit(payload: dict[str, Any]) -> float | None:
    """P30：模拟上游回传扣费金额。

    - 请求里带 `_mock_credit` → 用它（chat 路径能透传到 mock）；
    - 否则用模块级 `DEFAULT_CREDIT`（responses 入口只透传 model/messages/tools，
      请求字段到不了这里，测试改用 monkeypatch 这个变量）。
    两者都没有就是 None —— 和真实上游「不给金额」时一致。
    """
    for value in (payload.get("_mock_credit"), DEFAULT_CREDIT):
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


async def stream_response(payload: dict[str, Any], model: str | None = None) -> AsyncIterator[str]:
    """构造 SSE 流式响应（yield 已格式化的 data: 行）。"""
    if payload.get("_mock_fail") == "midstream":
        # 模拟：先吐一个 chunk 后断流，用于验证"断流不记成功"
        yield "data: " + json.dumps(
            {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": payload.get("model"),
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": "半截"}, "finish_reason": None}]},
            ensure_ascii=False,
        ) + "\n\n"
        raise UpstreamError("midstream_disconnect")

    if payload.get("_mock_fail") == "timeout":
        raise UpstreamError("timeout")

    model = payload.get("model") or settings.public_model
    text = _echo_text(payload)
    # 把文本切成片段，验证参数/内容分片场景
    parts = [text[i : i + 8] for i in range(0, len(text), 8)] or [""]
    for i, part in enumerate(parts):
        chunk = {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": part} if i == 0 else {"content": part},
                    "finish_reason": None,
                }
            ],
        }
        yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"

    # 结束帧
    end = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    # P30：模拟真实上游「最后一帧带 usage.credit」，用来测流式记账。
    # 只在请求里显式带 _mock_credit 时输出，避免影响既有断言。
    credit = _mock_usage_credit(payload)
    if credit is not None:
        end["usage"] = {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "credit": credit,
        }
    yield "data: " + json.dumps(end, ensure_ascii=False) + "\n\n"
    yield "data: [DONE]\n\n"
