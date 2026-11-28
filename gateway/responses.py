"""Responses API 端点（P7 客户端接入必需，P8 加固）。

一手依据（本机 Codex CLI 0.142.3 二进制 strings 提取，非猜测）：
- 二进制明确报 "wire_api = \"chat\" is no longer supported" / "set wire_api = \"responses\""
  → Codex 只走 Responses 协议，必须实现 /v1/responses。
- 事件名：response.created / response.in_progress / response.completed / response.failed /
  response.output_item.done / response.output_text.delta 等。
- ResponseItem 类型：message / reasoning / function_call / function_call_output /
  local_shell_call / custom_tool_call / web_search_call 等。
- 关键：二进制含 "previous_response_id"（"If you use previous_response_id, the API
  preserves prior assistant state"）→ 多轮续接必须支持，这正是调研指出
  qoder2api-hub 缺失的能力。

设计：
- 非流式：返回完整 Response object（output 数组含 message/content parts）。
- 流式：SSE 事件序列，客户端（Codex）按事件类型聚合。
- previous_response_id：服务端保存 Response 快照，续接时把历史 output 并入 input。
- 工具上下文：function_call → 客户端执行 → function_call_output 原样保留（复用 P3 透传原则）。

P8 自测修复（对抗性测试实证，非猜测）：
- BUG-1: 本端点原漏掉 API Key 校验，可无 key 调用（Codex 恰好走这里）→ 统一走 auth。
- BUG-2: 上下文/路由失败原直接抛异常不落请求记录 → 记录提前到校验前，统一 finalize。
- BUG-3: previous_response_id 续接原只并入 message，丢掉 function_call 工具骨架 →
  改为 input 与续接共用 _item_to_message，原样保留工具调用关联。
- BUG-4: 流式断连原留 pending 记录 → 加 is_disconnected 检测 + finally 兜底记 cancelled。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import mock_upstream
from .auth import check_api_key
from .config import settings
from .context import ContextError, check_and_passthrough
from .router import Candidate, Router
from .store import store

# P10：与 chat 端点共用同一上游选择（mock / 真实 WorkBuddy）
if settings.upstream == "workbuddy":
    from . import workbuddy_upstream as UPSTREAM  # type: ignore[assignment]
else:
    UPSTREAM = mock_upstream
router = Router()

# 会话续接存储：response_id -> {"output": [...], "model": ..., "created_at": ...}
# P7 单进程验证用内存；服务器多实例时改存 SQLite（表结构后续加）。
_RESPONSES: dict[str, dict[str, Any]] = {}
_MAX_RESPONSES = 1000  # P8 加固：防止长跑内存无限增长


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _save_snapshot(resp_id: str, output: list[dict[str, Any]], model: str) -> None:
    """保存续接快照，超上限时淘汰最旧的（P8）。"""
    _RESPONSES[resp_id] = {"output": output, "model": model, "created_at": time.time()}
    if len(_RESPONSES) > _MAX_RESPONSES:
        overflow = len(_RESPONSES) - _MAX_RESPONSES
        oldest = sorted(_RESPONSES.items(), key=lambda kv: kv[1]["created_at"])[:overflow]
        for key, _ in oldest:
            _RESPONSES.pop(key, None)


def _item_to_message(item: Any, default_role: str = "assistant") -> dict[str, Any] | None:
    """Responses item → 内部 chat message，原样保留工具骨架（P8 修复 BUG-3）。

    previous_response_id 续接与本次 input 走同一转换，避免续接时漏掉
    function_call / function_call_output 导致工具往返断裂——这正是本项目
    相对 ChinaCPA 的核心差异点，不能在自己这里复现。
    """
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")

    # 工具调用：保留 id/name/arguments，供客户端执行后回传 tool 结果
    if item_type == "function_call":
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": item.get("call_id", ""),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "{}"),
                },
            }],
        }
    # 工具结果：保留 call_id 关联
    if item_type == "function_call_output":
        return {
            "role": "tool",
            "tool_call_id": item.get("call_id", ""),
            "content": item.get("output", ""),
        }
    if item_type == "message" or item_type is None:
        content = item.get("content")
        # P11 实测：Codex Responses 用 input_text content part；P12 实测：工具回合
        # 续接时我们自己保存的 output 用 output_text。WorkBuddy 上游只接受
        # text/image_url（400 code=11101 unsupported content type）。
        # 这里做协议级类型映射：两种文本都映射为 text，正文原样保留、不裁剪。
        if isinstance(content, list):
            normalized = []
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("input_text", "output_text"):
                    p = dict(part)
                    p["type"] = "text"
                    normalized.append(p)
                else:
                    normalized.append(part)
            content = normalized
        # P13 修复：新版 Codex（App 内置 0.167）把 system 指令放在 developer
        # 角色里；Chat Completions 上游只接受 system/user/assistant/tool。
        # 这是协议角色适配（与 input_text→text 同类），正文原样保留、不裁剪，
        # 不伪装任何客户端身份。实测 developer 角色会被上游 11128 拒绝，
        # 映射为等价的 system 后即通过。
        role = item.get("role", default_role)
        if role == "developer":
            role = "system"
        return {"role": role, "content": content}
    # reasoning 等未知类型：不静默丢弃，降级为文本保留，避免结构校验失败
    return {
        "role": "assistant",
        "content": str(item.get("content") or item.get("summary") or ""),
    }


def _messages_from_input(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把 Responses input + instructions + previous_response_id 转成内部 chat messages。"""
    msgs: list[dict[str, Any]] = []
    if payload.get("instructions"):
        msgs.append({"role": "system", "content": payload["instructions"]})

    prev = payload.get("previous_response_id")
    if prev:
        snap = _RESPONSES.get(prev)
        if snap is None:
            raise HTTPException(status_code=404, detail=f"previous_response_id '{prev}' not found")
        # 续接：上轮 output 原样并入（工具骨架完整保留，不重建、不裁剪）
        for item in snap.get("output", []):
            msg = _item_to_message(item, "assistant")
            if msg is not None:
                msgs.append(msg)

    inp = payload.get("input")
    if isinstance(inp, str):
        msgs.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            msg = _item_to_message(item, "user")
            if msg is not None:
                msgs.append(msg)
    return msgs


def _build_response_obj(model: str, output: list[dict[str, Any]], status: str = "completed",
                        resp_id: str | None = None) -> dict[str, Any]:
    return {
        "id": resp_id or _new_id("resp"),
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": model,
        "output": output,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }


def _assistant_output(text: str) -> list[dict[str, Any]]:
    """构造 Responses 的 output：一条 message，含 output_text content part。"""
    return [{
        "id": _new_id("msg"),
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }]


def _tools_to_chat(tools: Any) -> list[dict[str, Any]]:
    """Responses tools → Chat Completions tools（P12 修复：原实现整段丢弃）。

    Codex 0.142 发的 function tool 形如
    {"type":"function","name":"...","description":"...","parameters":{...}}
    上游只认 Chat Completions 的 {"type":"function","function":{...}}。
    只做协议形状转换，不裁剪 description / parameters 任何内容。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return out
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = fn.get("name")
        if not name:
            continue
        item: dict[str, Any] = {"type": "function", "function": {"name": name}}
        for key in ("description", "parameters", "strict"):
            if key in fn:
                item["function"][key] = fn[key]
        out.append(item)
    return out


def _upstream_result(payload: dict[str, Any], messages: list[dict[str, Any]],
                     model: str, account: str | None = None,
                     upstream_model: str | None = None) -> dict[str, Any]:
    """走统一上游契约；把 tools 一并带上（P12 修复：此前 Codex 看不到任何工具）。

    P19：多账号时按路由结果指定凭据（account 为凭据文件标签），
    在 contextvar 里设置，适配器内部 _load_credential() 自动读对应文件。
    """
    chat_payload: dict[str, Any] = {
        "model": model or settings.public_model,
        "messages": messages,
        "tools": _tools_to_chat(payload.get("tools")),
    }
    # tool_choice 刻意不透传：WorkBuddy 上游只验证过默认 auto 行为，
    # 强行透传可能触发 11101/11128；不猜、不冒险。
    # P35：模型由调度器决定（可能不是后台当前选中的那个），真实上游要把
    # 实际用的模型 id 显式传下去。mock 模式不传（替身是单参函数）。
    if settings.upstream == "workbuddy" and account:
        from .workbuddy_upstream import reset_account, use_account
        ctx = use_account(account)
        try:
            if upstream_model:
                return UPSTREAM.build_nonstream_response(chat_payload, model=upstream_model)
            return UPSTREAM.build_nonstream_response(chat_payload)
        finally:
            reset_account(ctx)
    if upstream_model and settings.upstream == "workbuddy":
        return UPSTREAM.build_nonstream_response(chat_payload, model=upstream_model)
    return UPSTREAM.build_nonstream_response(chat_payload)


def _credit_of(upstream: dict[str, Any]) -> float | None:
    """取上游 usage.credit（P30）。

    Responses 入口不管流式还是非流式，底层都是「非流式聚合」拿到整只 chat 响应，
    所以 usage 就在手上，不需要嗅探 SSE 帧。没有 credit 就是 None（不猜）。
    """
    from .credit import credit_from_usage
    if not isinstance(upstream, dict):
        return None
    return credit_from_usage(upstream.get("usage"))


def _response_output_from_chat(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Chat message（含 tool_calls）→ Responses output items，供续接与客户端执行。"""
    msg = result.get("choices", [{}])[0].get("message") or {}
    output: list[dict[str, Any]] = []
    text = msg.get("content")
    if text or not msg.get("tool_calls"):
        output.append({
            "id": _new_id("msg"), "type": "message", "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text or "", "annotations": []}],
        })
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        output.append({
            "type": "function_call",
            "id": tc.get("id") or _new_id("fc"),
            "call_id": tc.get("id") or "",
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", "{}"),
            "status": "completed",
        })
    return output


async def responses_endpoint(request: Request) -> Any:
    check_api_key(request)  # P8 修复 BUG-1：与 /v1/chat/completions 统一鉴权

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    model = payload.get("model") or settings.low_model
    # R1：只接受已发布的对外模型名（low/high）
    _mode = settings.mode_of(model)
    if not _mode:
        raise HTTPException(status_code=404, detail=f"model '{model}' not found; published: {settings.published_models()}")
    from .app import use_mode
    use_mode(_mode)

    # P8 修复 BUG-2：记录请求提前到校验之前，失败/404 也能落库审计
    req_id = store.record_request(
        public_model=model,
        stream=bool(payload.get("stream")),
        payload={
            "model": model,
            "input": payload.get("input"),
            "tools": payload.get("tools", []),
            "previous_response_id": payload.get("previous_response_id"),
        },
    )
    started = time.monotonic()
    messages: list[dict[str, Any]] = []  # 供 finalize 估算 token，校验失败时为空
    fields: dict[str, Any] = {}
    finalized = {"done": False}
    chosen_ctx: dict[str, Any] = {}   # R1：chosen 确定后填入，供 finalize 里的 high 提醒读

    def finalize(status: str, error: str | None = None, **extra: Any) -> None:
        # 与 chat 端点一致：任何退出路径恰好落一次终态
        if finalized["done"]:
            return
        finalized["done"] = True
        # R1 提醒（high）：请求成功后检查账号余额是否跌破阈值。
        if status == "success" and _mode == "high" and chosen_ctx:
            try:
                from . import notify
                notify.check_low_balance(
                    account=chosen_ctx.get("account") or "",
                    platform=chosen_ctx.get("platform") or "",
                    balance=chosen_ctx.get("balance"),
                )
            except Exception:
                pass
        store.finalize_request(
            req_id, status=status, error=error,
            latency_ms=int((time.monotonic() - started) * 1000),
            token_estimate=sum(len(str(m.get("content", ""))) // 4 for m in messages),
            platform=fields.get("platform"), model_name=fields.get("model_name"),
            account_label=fields.get("account_label"), choose_reason=fields.get("choose_reason"),
            **extra,
        )

    # 上下文构造 + 校验（复用 P3）
    try:
        messages = _messages_from_input(payload)
        check_and_passthrough(
            {"model": model, "messages": messages}, context_window=settings.context_window
        )
    except ContextError as e:
        finalize("error", f"context:{e}")
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException as e:
        finalize("error", f"input:{e.detail}")
        raise

    # 路由决策（复用 P4）
    need_tools = bool(payload.get("tools"))
    need_image = any(
        isinstance(m.get("content"), list)
        and any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
        for m in messages if isinstance(m, dict)
    )
    plan = _route(need_tools, need_image, messages)
    if not plan.candidates:
        finalize("error", f"route:{plan.reason}")
        raise HTTPException(status_code=503, detail=plan.reason)
    chosen = plan.candidates[0]
    fields.update({
        "platform": chosen.platform,
        "model_name": chosen.model_name,
        "account_label": chosen.account_label,
        "choose_reason": plan.reason,
    })
    # R2.22：路由一确定就回填到 pending 记录，进行中的明细行也显示真实模型名。
    store.attach_route(req_id, **fields)
    chosen_ctx.update(account=chosen.account_label, platform=chosen.platform,
                      balance=chosen.balance)
    # R1 提醒（low）：与 chat 端点一致——选中收费模型即尝试当天首封提醒。
    if _mode == "low" and chosen.est_cost is not None and chosen.est_cost > 0:
        try:
            from . import notify
            from .scheduler import free_fallback_summary
            notify.note_paid_start(
                model=chosen.model_name, account=chosen.account_label,
                cost=chosen.est_cost, balance=chosen.balance,
                daily_total=store.credit_today(),
                price_factor=chosen.price_factor,
                choose_reason=plan.reason,
                free_alternatives=free_fallback_summary(plan.rejected),
            )
        except Exception:
            pass
    # P36：事实块（说清当前实际用的模型）
    try:
        from . import facts
        messages = facts.inject({"messages": messages}, model=chosen.model_name)["messages"]
    except Exception:
        pass

    if payload.get("stream"):
        async def event_stream() -> AsyncIterator[str]:
            resp_id = _new_id("resp")
            msg_id = _new_id("msg")
            outcome = "cancelled"  # P8 修复 BUG-4：未正常收尾一律不记成功
            outcome_error: str | None = "client_disconnected_or_incomplete"
            actual_credit: float | None = None  # P30：流式也记账

            def evt(event: str, data: dict[str, Any]) -> str:
                d = dict(data)
                d.setdefault("type", event)
                return f"event: {event}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n"

            async def emit(event: str, data: dict[str, Any]) -> str | None:
                # 每个事件前检测客户端是否已断开；断开则停止产出
                if await request.is_disconnected():
                    return None
                return evt(event, data)

            try:
                created = _build_response_obj(model, [], "in_progress", resp_id)
                for event, data in (
                    ("response.created", {"response": created}),
                    ("response.in_progress", {"response": created}),
                ):
                    chunk = await emit(event, data)
                    if chunk is None:
                        return  # finally 记 cancelled
                    yield chunk

                item = {"id": msg_id, "type": "message", "status": "in_progress",
                        "role": "assistant", "content": []}
                part = {"type": "output_text", "text": "", "annotations": []}
                for event, data in (
                    ("response.output_item.added", {"output_index": 0, "item": item}),
                    ("response.content_part.added", {
                        "item_id": msg_id, "output_index": 0, "content_index": 0, "part": part,
                    }),
                ):
                    chunk = await emit(event, data)
                    if chunk is None:
                        return
                    yield chunk

                upstream = _call_upstream_with_retry(payload, messages, model, plan)
                actual_credit = _credit_of(upstream)
                assistant = upstream.get("choices", [{}])[0].get("message") or {}
                full = assistant.get("content") or ""
                output_items: list[dict[str, Any]] = []

                parts = [full[i:i + 8] for i in range(0, len(full), 8)] or [""]
                for p in parts:
                    chunk = await emit("response.output_text.delta", {
                        "item_id": msg_id, "output_index": 0, "content_index": 0, "delta": p,
                    })
                    if chunk is None:
                        return
                    yield chunk

                final_part = {"type": "output_text", "text": full, "annotations": []}
                final_item = {"id": msg_id, "type": "message", "status": "completed",
                              "role": "assistant", "content": [final_part]}
                for event, data in (
                    ("response.output_text.done", {
                        "item_id": msg_id, "output_index": 0, "content_index": 0, "text": full,
                    }),
                    ("response.content_part.done", {
                        "item_id": msg_id, "output_index": 0, "content_index": 0, "part": final_part,
                    }),
                    ("response.output_item.done", {"output_index": 0, "item": final_item}),
                ):
                    chunk = await emit(event, data)
                    if chunk is None:
                        return
                    yield chunk
                output_items.append(final_item)

                # P12 修复：上游 tool_calls → Responses function_call SSE 事件。
                # Codex 靠 call_id / name / arguments 执行本地工具并回传
                # function_call_output；缺了这一段 agent 就只能"口头说没法做"。
                for tc in assistant.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    call_id = tc.get("id") or _new_id("fc")
                    name = fn.get("name", "")
                    arguments = fn.get("arguments", "{}")
                    idx = len(output_items)
                    base_item = {
                        "type": "function_call", "id": call_id, "call_id": call_id,
                        "name": name, "arguments": "", "status": "in_progress",
                    }
                    chunk = await emit("response.output_item.added", {
                        "output_index": idx, "item": base_item,
                    })
                    if chunk is None:
                        return
                    yield chunk
                    for piece in [arguments[i:i + 8] for i in range(0, len(arguments), 8)]:
                        chunk = await emit("response.function_call_arguments.delta", {
                            "item_id": call_id, "output_index": idx, "delta": piece,
                        })
                        if chunk is None:
                            return
                        yield chunk
                    complete_item = dict(base_item, arguments=arguments, status="completed")
                    for event, data in (
                        ("response.function_call_arguments.done", {
                            "item_id": call_id, "output_index": idx, "arguments": arguments,
                        }),
                        ("response.output_item.done", {"output_index": idx, "item": complete_item}),
                    ):
                        chunk = await emit(event, data)
                        if chunk is None:
                            return
                        yield chunk
                    output_items.append(complete_item)

                result = _build_response_obj(model, output_items, "completed", resp_id)
                _save_snapshot(resp_id, output_items, model)
                chunk = await emit("response.completed", {"response": result})
                if chunk is None:
                    return
                yield chunk

                outcome, outcome_error = "success", None
            except Exception as e:  # noqa: BLE001
                outcome, outcome_error = "error", str(e)
                try:
                    yield evt("response.failed", {
                        "response": {"type": "response", "status": "failed",
                                     "error": {"message": str(e)}}
                    })
                except Exception:
                    pass
            finally:
                # 正常/异常/客户端断开/生成器被关闭，都恰好落一次终态
                finalize(outcome, outcome_error, actual_credit=actual_credit)

        return StreamingResponse(
            event_stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式
    try:
        upstream = _call_upstream_with_retry(payload, messages, model, plan)
        output = _response_output_from_chat(upstream)
        result = _build_response_obj(model, output, "completed")
        _save_snapshot(result["id"], output, model)
        finalize("success", actual_credit=_credit_of(upstream))
        return JSONResponse(result)
    except Exception as e:  # noqa: BLE001
        finalize("error", str(e))
        raise HTTPException(status_code=500, detail="internal error")


def _call_upstream_with_retry(payload: dict[str, Any], messages: list[dict[str, Any]],
                              model: str, plan) -> dict[str, Any]:
    """按候选顺序调用上游；遇到可重试的错误就冷却并换下一个候选（P35）。

    不可重试的两类（11128 渠道风控 / 11101 格式错误）直接抛出去，
    否则会变成重试风暴（这两类历史上占 400 错误的大头）。
    """
    from . import scheduler as SCHED

    cands = list(plan.candidates)
    last: Exception | None = None
    for i, cand in enumerate(cands):
        try:
            return _upstream_result(
                payload, messages, model, cand.account_label, cand.model_name
            )
        except Exception as exc:  # noqa: BLE001
            last = exc
            reason = str(exc)
            if i < len(cands) - 1 and SCHED.is_retryable(reason):
                SCHED.mark_cooldown(cand, SCHED.cooldown_for(reason), reason)
                try:
                    store.log_schedule(
                        "retry",
                        f"{cand.account_label}/{cand.model_name} 失败（{reason[:100]}），"
                        f"换第 {i + 2} 个候选（共 {len(cands)} 个）",
                        "warn",
                    )
                except Exception:
                    pass
                continue
            raise
    if last is not None:
        raise last
    raise RuntimeError("no candidate")


def _route(need_tools: bool, need_image: bool, messages: list[dict[str, Any]]):
    """候选计划（P35：与 chat 端点同一套调度器）。

    返回的是 Plan（含排序后的候选列表），Responses 入口按顺序尝试，
    上游失败就换下一个候选。
    """
    from .app import build_plan
    return build_plan(
        {"messages": messages},
        need_tools=need_tools, need_image=need_image,
        need_context=sum(len(str(m.get("content", ""))) // 4 for m in messages),
    )
