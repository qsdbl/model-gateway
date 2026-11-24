"""上下文透传（P3）：完整保留工具与图片，不裁剪、不静默丢弃。

调研报告核心差异点：ChinaCPA 的 Qoder 适配器把历史重建为"角色+纯文本"，
丢失工具调用关联和图片 —— 本项目必须反其道而行：

原则（README 第5节 P3）：
- 由客户端提供完整消息历史，网关原样重放；
- 不删 system、tool 结果、image_url；
- 结构性问题（缺 tool_call_id、损坏工具流）明确报错；
- 上下文超限明确提示，不静默截断；
- 内容字段白名单外明确报错；客户端私有元数据字段剥离（见下方说明）。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class ContextError(ValueError):
    """上下文结构不合法或不被支持。携带明确原因给客户端。"""


VALID_ROLES = {"system", "user", "assistant", "tool", "developer"}

# 本网关明确支持的消息字段（内容字段白名单）。
_SUPPORTED_MSG_KEYS = {"role", "content", "tool_calls", "tool_call_id", "name"}

# OpenAI 协议里真实存在、但本网关不支持的**内容**字段 —— 这些必须明确报错，
# 不能当元数据剥掉（否则会静默丢失用户内容）。
_UNSUPPORTED_CONTENT_KEYS = frozenset({
    "audio",            # assistant 音频输出
    "function_call",    # 旧版工具调用，可能带内容
    "input_audio",      # user 音频输入
})

# 已知的客户端元数据字段（仅用于日志分类；实际剥离规则见 strip_client_metadata）。
# 2026-11-20 实测出现的字段：
#   ① user 消息：agent / conversationRequestId / startsNewUserRequest / entryId
#   ② assistant 回放：messageId / model / rawUsage / reasoning / requestModelId /
#      requestModelName / stepSeq / traceId / usage / argumentsDisplayText
#   ③ 客户端本地标记：skipRun / error / status / sensitiveUserInputReviewed / _meta
_KNOWN_METADATA_KEYS = frozenset({
    "agent", "conversationRequestId", "startsNewUserRequest", "entryId",
    "messageId", "model", "rawUsage", "reasoning", "requestModelId",
    "requestModelName", "stepSeq", "traceId", "usage", "argumentsDisplayText",
    "skipRun", "error", "status", "sensitiveUserInputReviewed",
    "baggage", "traceparent", "__codebuddyLocal", "_meta",
})

# 剥离上限：一条消息被剥掉超过这个数量的字段 → 视为异常，明确报错。
# 防止「客户端整个请求发错了格式，网关却假装支持、悄悄把内容全丢掉」。
_MAX_STRIPPED_KEYS_PER_MSG = 32


def strip_client_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """剥掉 message 上的客户端私有字段，返回新 payload（不就地修改入参）。

    策略（2026-11-20 定稿，替代逐字段补名单的打地鼠）：
    - 白名单字段（role/content/tool_calls/tool_call_id/name）原样保留，正文一字不改；
    - `_UNSUPPORTED_CONTENT_KEYS` 里的内容字段 → 抛 ContextError（不静默丢内容）；
    - 其余字段一律视为客户端私有元数据剥掉，并写 debug 日志；
    - 单条消息剥离字段过多 → 抛 ContextError，防止异常格式被吞掉。

    原因：WorkBuddy CLI 在不同场景会带不同的私有字段（会话追踪、assistant 回放、
    工具展示文本、context entry id……），逐字段补名单已连踩三轮（agent →
    messageId → entryId/argumentsDisplayText），不可能穷举。
    """
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return payload
    cleaned: list[Any] = []
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            cleaned.append(m)
            continue
        content_fields = set(m.keys()) & _UNSUPPORTED_CONTENT_KEYS
        if content_fields:
            raise ContextError(
                f"messages[{i}] has unsupported content field(s): "
                f"{sorted(content_fields)}"
            )
        extra = set(m.keys()) - _SUPPORTED_MSG_KEYS
        if not extra:
            cleaned.append(m)
            continue
        if len(extra) > _MAX_STRIPPED_KEYS_PER_MSG:
            raise ContextError(
                f"messages[{i}] has {len(extra)} unknown field(s) — "
                f"refusing to strip (possible malformed request)"
            )
        if log.isEnabledFor(logging.DEBUG):
            known = sorted(extra & _KNOWN_METADATA_KEYS)
            unknown = sorted(extra - _KNOWN_METADATA_KEYS)
            log.debug(
                "strip client metadata from messages[%d]: known=%s unknown=%s",
                i, known, unknown,
            )
        cleaned.append({k: v for k, v in m.items() if k not in extra})
    return {**payload, "messages": cleaned}


def check_and_passthrough(payload: dict[str, Any], *, context_window: int | None = None) -> dict[str, Any]:
    """校验上下文结构并原样返回（透传，不做任何内容改写）。

    先剥掉客户端私有元数据（WorkBuddy 会话追踪字段），再校验。
    返回的是清洗后 payload 的浅拷贝（messages 列表原样引用）—— 保证"重放"而非"重建"。
    任何结构问题抛 ContextError，附带明确原因。
    """
    payload = strip_client_metadata(payload)
    msgs = payload.get("messages")
    if not isinstance(msgs, list) or not msgs:
        raise ContextError("messages must be a non-empty array")

    # 收集 assistant 发出的 tool_call id，用于校验 tool 结果的关联
    emitted_calls: set[str] = set()

    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            raise ContextError(f"messages[{i}] must be an object")

        role = m.get("role")
        if role not in VALID_ROLES:
            raise ContextError(f"messages[{i}].role '{role}' is not supported")

        # 字段清洗已在 strip_client_metadata() 完成：内容字段白名单外的明确报错，
        # 客户端私有元数据已剥离。这里只校验结构本身。

        content = m.get("content")

        # assistant 的 tool_calls：记录 id，保证结构完整
        if role == "assistant" and "tool_calls" in m:
            tcs = m["tool_calls"]
            if not isinstance(tcs, list):
                raise ContextError(f"messages[{i}].tool_calls must be an array")
            for j, tc in enumerate(tcs):
                if not isinstance(tc, dict) or tc.get("type") != "function":
                    raise ContextError(
                        f"messages[{i}].tool_calls[{j}] must be a function call object"
                    )
                fn = tc.get("function")
                if not isinstance(fn, dict) or "name" not in fn:
                    raise ContextError(
                        f"messages[{i}].tool_calls[{j}].function.name is required"
                    )
                call_id = tc.get("id")
                if not isinstance(call_id, str) or not call_id:
                    raise ContextError(
                        f"messages[{i}].tool_calls[{j}].id is required "
                        "for tool result correlation"
                    )
                emitted_calls.add(call_id)

        # tool 结果：必须有 tool_call_id 且能对应到之前的调用
        if role == "tool":
            tcid = m.get("tool_call_id")
            if not isinstance(tcid, str) or not tcid:
                raise ContextError(
                    f"messages[{i}].tool_call_id is required for tool result"
                )
            if tcid not in emitted_calls:
                raise ContextError(
                    f"messages[{i}].tool_call_id '{tcid}' has no matching "
                    "assistant tool_calls — broken tool flow"
                )

        # 图片 content：允许并保留，不剥离
        if isinstance(content, list):
            for j, part in enumerate(content):
                if not isinstance(part, dict):
                    raise ContextError(f"messages[{i}].content[{j}] must be an object")
                ptype = part.get("type")
                if ptype == "image_url":
                    iu = part.get("image_url")
                    if not isinstance(iu, dict) or not iu.get("url"):
                        raise ContextError(
                            f"messages[{i}].content[{j}].image_url.url is required"
                        )
                elif ptype not in ("text", "input_text", "output_text"):
                    raise ContextError(
                        f"messages[{i}].content[{j}].type '{ptype}' is not supported"
                    )

    # 上下文超限：明确提示，不静默截断
    if context_window is not None and context_window > 0:
        approx = _approx_tokens(msgs)
        if approx > context_window:
            raise ContextError(
                f"context overflow: ~{approx} tokens exceed model limit "
                f"{context_window}; refusing to silently truncate"
            )

    return dict(payload)  # 浅拷贝：messages 原样引用 = 真正重放


def _approx_tokens(msgs: list[dict[str, Any]]) -> int:
    """保守估算（含工具/图片占位），仅用于超限保护，不是账单。"""
    total = 0
    for m in msgs:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c) // 4
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += len(str(part.get("text", ""))) // 4
                elif isinstance(part, dict) and part.get("type") == "image_url":
                    total += 256  # 图片保守占位
        if m.get("tool_calls"):
            total += len(str(m["tool_calls"])) // 4
    return total


def tool_round_signature(msgs: list[dict[str, Any]]) -> str:
    """生成当前工具回合的签名，用于"同一工具往返内保持稳定"判定（P4 用）。

    包含 system + 所有 tool_call id + tool 结果，任何变化都视为新回合。
    """
    import hashlib
    import json

    skeleton = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        tcs = [tc.get("id") for tc in (m.get("tool_calls") or []) if isinstance(tc, dict)]
        # 只纳入工具骨架相关消息：system/developer、带 tool_calls 的 assistant、
        # 以及 tool 结果。普通对话消息不参与，保证同一工具回合内签名稳定。
        if role in ("system", "developer") or tcs or role == "tool":
            skeleton.append({
                "role": role if role in ("system", "developer") else "tool_flow",
                "tool_call_ids": tcs,
                "tool_call_id": m.get("tool_call_id"),
            })
    raw = json.dumps(skeleton, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
