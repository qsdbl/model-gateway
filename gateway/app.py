"""Personal Model Gateway - FastAPI 应用。

P2 范围：
- GET  /v1/models            OpenAI 兼容模型列表（R1 起为 low + high 两个）
- POST /v1/chat/completions  非流式 + SSE 流式（mock 上游）
- 请求记录：开始→结束的完整生命周期；失败/断流/取消不记成功
- 不支持字段明确报错，不假装支持

P3+ 会在此基础上加工具上下文与真实调度。
R1：双模式模型名——客户端用 model=low 或 high 决定本次的调度策略，
    个人直连 IP 部署（P22）不变。
"""
from __future__ import annotations

import contextvars
import json
import re
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__, mock_upstream
from . import facts
from .auth import check_api_key
from .config import settings
from .context import ContextError, check_and_passthrough
from .router import Candidate, Router
from .store import store

# 兼容别名：P2 起的既有调用名保留，实际实现统一在 auth 模块（P8 修复）
_check_api_key = check_api_key

# 路由器单例（P4）。P7 接入真实平台后由适配器提供候选。
router = Router()

# R1：本次请求的模式（low/high）。contextvar 而非全局，避免并发串模式。
# 适配器据此把响应里的 model 字段回写成客户端请求的那个名字。
_REQUEST_MODE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "gateway_request_mode", default="low"
)


def use_mode(mode: str):
    """设置本次请求模式，返回 token 供 finally 里 reset。"""
    return _REQUEST_MODE.set(mode)


def reset_mode(token) -> None:
    _REQUEST_MODE.reset(token)


def current_mode() -> str:
    return _REQUEST_MODE.get()


# 价格缓存注入点（P5）。P7 由真实平台查询函数构造并注入。
_price_cache = None


def set_price_cache(cache) -> None:
    """注入价格/余额缓存（P5/P7）。"""
    global _price_cache
    _price_cache = cache


def err_brief(exc: BaseException) -> str:
    """内部异常的落库摘要：类型 + 一句原因。

    只写 `internal:类型名` 的话，线上 500 事后完全没法查（P2 起攒下的 4 条
    internal:AttributeError / TypeError 至今不知道出在哪一行）。带上异常消息，
    但压掉换行并截断，别让一坨 payload 把记录撑爆。凭据只在 header 里传、
    不参与解析，不会出现在异常消息里。
    """
    msg = re.sub(r"\s+", " ", str(exc)).strip()[:160]
    name = type(exc).__name__
    return f"internal:{name}: {msg}" if msg else f"internal:{name}"


def stream_latency_ms(started: float, ended_at: float | None,
                      last_event_at: float) -> int:
    """R2.17：算请求耗时时，绝不把「生成器被 GC 的时刻」当结束时刻。

    StreamingResponse 的异步生成器在客户端断开后可能很久才被回收；旧代码在
    `finally` 里直接 `time.monotonic() - started`，于是 10:25 的一条取消请求
    被记成 44853 秒，管理页「最慢」一栏全是假数据，直接把排障带偏。

    取值优先级：显式记录的结束时刻 > 最后一帧发出的时刻。二者都没有时用 started
    （宁可记 0，也不要记成几小时）。
    """
    end = ended_at if ended_at is not None else last_event_at
    return max(0, int((end - started) * 1000))


def credit_from_sse_chunk(chunk: str) -> float | None:
    """兼容旧引用：实现搬到 gateway/credit.py（P30）。"""
    from .credit import credit_from_sse_chunk as _impl
    return _impl(chunk)


def prompt_tokens_from_sse_chunk(chunk: str) -> int | None:
    """从 SSE 帧里取真实输入 token（P41）。"""
    from .credit import prompt_tokens_from_sse_chunk as _impl
    return _impl(chunk)


def chunk_has_finish_reason(chunk: str) -> bool:
    """R2.40：这一帧是否带非空 finish_reason（内容已完整吐出）。"""
    from .credit import chunk_has_finish_reason as _impl
    return _impl(chunk)


def chunk_diag_label(chunk: str) -> str:
    """R2.48：把一帧压成短标签供断流诊断日志用（只观测，不改判据）。"""
    from .credit import chunk_diag_label as _impl
    return _impl(chunk)

def chunk_content_len(chunk: str) -> int:
    """R2.51：这一帧真正的 delta.content 字符数（只观测，不参与判据）。"""
    from .credit import chunk_content_len as _impl
    return _impl(chunk)


# ---------- P35 换号重试的三个小工具 ----------
def _is_retryable(reason: str) -> bool:
    from .scheduler import is_retryable

    return is_retryable(reason)


def _cool_down(cand, reason: str) -> None:
    from .scheduler import cooldown_for, mark_cooldown

    try:
        mark_cooldown(cand, cooldown_for(reason), reason)
    except Exception:
        pass


def _log_switch(cand, reason: str, attempt: int, total: int) -> None:
    try:
        store.log_schedule(
            "retry",
            f"{cand.account_label}/{cand.model_name} 失败（{reason[:100]}），"
            f"换第 {attempt + 1} 个候选（共 {total} 个）",
            "warn",
        )
    except Exception:
        pass


def _note_used(cand) -> None:
    from .scheduler import note_used

    try:
        note_used(cand)
    except Exception:
        pass


def _stream_upstream(payload: dict[str, Any], cand):
    """按候选指定的**平台**打开上游流。

    P44：现在有两个上游了，Qoder 走 `qoderclicn` 子进程（不是 HTTP）。
    分派依据是候选自己的 platform，不是全局的 settings.upstream ——
    调度器可能在 WorkBuddy 和 Qoder 之间自动切换。

    只有真实上游（workbuddy）才把模型名传下去：mock 模式下模型名没有意义，
    而且测试替身是单参函数，无条件传 model= 会破坏它们。
    """
    if getattr(cand, "platform", "workbuddy") == "qoder":
        from . import qoder_upstream as qu
        return qu.stream_response(payload, model=cand.model_name)
    if settings.upstream == "workbuddy" and cand.model_name:
        return UPSTREAM.stream_response(payload, model=cand.model_name)
    return UPSTREAM.stream_response(payload)


async def _nonstream_upstream(payload: dict[str, Any], cand):
    """非流式：按候选平台分派。

    P44：Qoder 的上游是子进程 + asyncio 实现的，**必须 await**。
    而 WorkBuddy 那版是同步函数（历史原因），直接调。
    所以这里统一成 async：Qoder 分支 await，WorkBuddy 分支原样返回。
    （踩过的坑：Qoder 那版写成 async 而调用处没 await →
      返回一个没被执行的协程对象，JSON 序列化直接报
      "Object of type coroutine is not JSON serializable"，
      上游其实已经跑成功了，客户端却收到 500。）
    """
    if getattr(cand, "platform", "workbuddy") == "qoder":
        from . import qoder_upstream as qu
        return await qu.build_nonstream_response(payload, model=cand.model_name)
    if settings.upstream == "workbuddy" and cand.model_name:
        return UPSTREAM.build_nonstream_response(payload, model=cand.model_name)
    return UPSTREAM.build_nonstream_response(payload)


def _use_account_for(cand):
    """按候选平台设置「当前账号」，返回可用于 reset 的 ctx。

    P44：Qoder 和 WorkBuddy 各有自己的 contextvar，两个都得能设能读
    ——P43 踩过的坑（contextvar 设了不读 = 功能完全没生效）不能再踩。
    """
    platform = getattr(cand, "platform", "workbuddy")
    if platform == "qoder":
        from . import qoder_upstream as qu
        return qu.use_account(cand.account_label)
    from .workbuddy_upstream import use_account
    return use_account(cand.account_label)


def _reset_account_for(cand, ctx) -> None:
    platform = getattr(cand, "platform", "workbuddy")
    if platform == "qoder":
        from . import qoder_upstream as qu
        qu.reset_account(ctx)
        return
    from .workbuddy_upstream import reset_account
    reset_account(ctx)


def _upstream_error_types() -> tuple:
    """两个上游各自的错误类型。

    P44：Qoder 的上游是子进程，抛的是 `qoder_upstream.UpstreamError`，
    和 WorkBuddy 的不是同一个类。只 catch WorkBuddy 那个的话，
    Qoder 的失败会掉进「未知异常」分支——错误信息变成 "internal error"，
    而且**不会触发换号重试**。
    """
    from . import qoder_upstream as qu
    return (UPSTREAM.UpstreamError, qu.UpstreamError)


# 候选提供者：默认 mock 候选；P7 用真实适配器覆盖（set_candidate_provider）。
_candidate_provider = None
# 真实 WorkBuddy 余额缓存（P10/P19）：label -> (monotonic, billing|None, meta|None)
_wb_balance_cache: dict[str, tuple[float, Any, Any]] = {}
# R2.44：上一次读账单的失败原因（label -> reason，成功则删掉）。
# 401 与 429/5xx 必须分开：401 是凭据被上游作废，只能重新扫码；
# 其余是暂时读不到，重试即可。调度器和账号页都读它来给准确文案。
_wb_balance_error: dict[str, str] = {}


def set_candidate_provider(fn) -> None:
    """注入真实候选提供者（P7）。签名: (payload) -> list[Candidate]。"""
    global _candidate_provider
    _candidate_provider = fn


def _account_snapshot(label: str):
    """某账号的 (账单, 元信息)，60s 缓存；失败也缓存，避免连环打上游。"""
    now = time.monotonic()
    hit = _wb_balance_cache.get(label)
    if hit and now - hit[0] <= 60:
        return hit[1], hit[2]
    from .workbuddy_upstream import account_meta, fetch_billing, reset_account, use_account
    ctx = use_account(label)
    try:
        billing = fetch_billing()
        _wb_balance_error.pop(label, None)
    except Exception as exc:  # noqa: BLE001
        billing = None
        _wb_balance_error[label] = str(exc)[:200]
    finally:
        reset_account(ctx)
    meta = account_meta(label)
    _wb_balance_cache[label] = (now, billing, meta)
    return billing, meta


def account_billing_error(label: str) -> str | None:
    """上次读该账号账单失败的原因（成功返回 None）。R2.44。

    只在上一次 `_account_snapshot()` 真的失败过（且没有被后续成功清掉）时有值。
    """
    return _wb_balance_error.get(label)


def billing_error_is_auth(label: str) -> bool:
    """这个失败是不是「凭据被上游作废」（401）——只有这种才该让用户重新扫码。

    429/超时/5xx 属于暂时读不到，重试就好；把它也标成「重新授权」是误报。
    """
    err = (_wb_balance_error.get(label) or "")
    return "HTTP 401" in err or "HTTP 403" in err


def get_candidates(payload: dict[str, Any]) -> list[Candidate]:
    if _candidate_provider is not None:
        return _candidate_provider(payload)
    if settings.upstream == "workbuddy":
        # P19：一个凭据文件一个候选，账号页看到的就是参与调度的全部账号。
        # 指定了「当前账号」就只用它（和手动选模型同一思路：显式覆盖自动调度）。
        from . import modelstate
        from .workbuddy_upstream import catalog, effective_model, known_accounts
        model_id = effective_model()
        info = next((r for r in catalog() if r.get("model_name") == model_id), None) or {}
        accounts = known_accounts()
        active_acc = modelstate.active_account()
        labels = [active_acc] if active_acc and active_acc in accounts else list(accounts)
        if not accounts:
            # 没有凭据文件（理论上不该发生）：返回一个状态未知的候选，
            # 让 Router 明确拒绝并给出「凭据未配置」，而不是抛异常糊掉请求。
            return [Candidate(
                account_label="未配置", platform="workbuddy", model_name=model_id,
                public_name=settings.public_model, balance=None,
                balance_unit="credits_workbuddy", auth_status="unknown", is_free=True,
                price_factor=1.0, promotion_until=None, tools=True, image=False,
                context_window=settings.context_window or 128000, price_fetched_at=None,
            )]
        enabled = {r["label"]: bool(r["enabled"]) for r in store.list_accounts()}
        out: list[Candidate] = []
        for label in labels:
            if enabled.get(label) is False:
                continue  # 后台显式停用的账号不参与调度
            billing, meta = _account_snapshot(label)
            out.append(Candidate(
                account_label=label, platform="workbuddy", model_name=model_id,
                public_name=settings.public_model,
                balance=(billing or {}).get("total"), balance_unit="credits_workbuddy",
                auth_status="ok" if billing else "unknown",
                is_free=bool(info.get("is_free", model_id.endswith("-free"))),
                price_factor=info.get("credits_factor") or 1.0, promotion_until=None,
                tools=bool(info.get("tools", True)), image=bool(info.get("image", False)),
                context_window=info.get("context_window") or settings.context_window or 128000,
                price_fetched_at=(billing or {}).get("checked_at"),
            ))
        if not out:
            # 全被停用：给一个 unknown 候选，交由 Router 拒绝（错误信息可读）
            out.append(Candidate(
                account_label="全部停用", platform="workbuddy", model_name=model_id,
                public_name=settings.public_model, balance=None,
                balance_unit="credits_workbuddy", auth_status="disabled", is_free=True,
                price_factor=1.0, promotion_until=None, tools=True, image=False,
                context_window=settings.context_window or 128000, price_fetched_at=None,
            ))
        return out
    # mock 默认候选：满足所有能力、免费、余额已知
    return [Candidate(
        account_label="mock-account", platform="mock", model_name=settings.public_model,
        public_name=settings.public_model, balance=1000.0, balance_unit="mock_credits",
        auth_status="ok", is_free=True, price_factor=1.0, promotion_until=None,
        tools=True, image=True, context_window=128000, price_fetched_at=None,
    )]


def build_plan(
    payload: dict[str, Any],
    *,
    need_tools: bool = False,
    need_image: bool = False,
    need_context: int = 0,
    est_tokens: int = 0,
    mode: str | None = None,
) -> "Plan":
    """本次请求的候选计划。

    真实上游（P35）走调度器：账号 × 模型二维池 + 预计扣费排序 + 轮转 + 冷却。
    mock 上游仍走旧 Router（本地开发没有真实账号池）。

    P40：手动指定模型改从**调度配置**读（`routes.json` 的 manual_model），
    不再用 P15 的 `modelstate.active_model()`。后者是「完全不走自动调度」的
    旧语义（后台点一行就锁死），留在调度路径上会让免费优先/轮转/付费兜底
    全部失效——线上就踩过这个坑：state.json 里残留一个调试时锁的模型，
    整个 P35 策略等于没上线，而且调度页看不见也清不掉。

    P41：`est_tokens` 是本次请求的预计输入 token 数（含事实块），
    调度器用它算每个模型的预计扣费——上游按 token 计费，这是唯一正确的判据。
    """
    from . import modelstate
    from . import scheduler as SCHED

    if settings.upstream != "workbuddy":
        decision = router.choose(
            get_candidates(payload),
            need_tools=need_tools, need_image=need_image,
            need_context=need_context,
            messages=payload.get("messages"),
            manual_model=modelstate.active_model(),
        )
        return SCHED.Plan(
            candidates=[decision.candidate] if decision.candidate else [],
            rejected=list(decision.rejected),
            reason=decision.reason,
        )
    cfg = router.load_config()
    manual = cfg.get("manual_model") or None
    # R1：mode 决定用哪套 profile（low=免费优先自动；high=指定强模型优先级）。
    return SCHED.build_plan(
        need_tools=need_tools, need_image=need_image, need_context=need_context,
        manual_model=manual, est_tokens=est_tokens,
        mode=mode or current_mode(),
    )

@asynccontextmanager
async def _lifespan(app):
    """P44：启动时清掉上一轮遗留的 Qoder 登录进程。

    服务重启时，正在等授权的 `qoderclicn login` 子进程会被孤儿化
    （PPID 变成 1，不跟着网关一起死），每个占 ~250MB。
    服务器总共 1.87GB，攒几个就把网关挤死了 —— 所以启动时按
    `data/qoder-cli/*/.login_pid` 里的 PID 收一遍。
    """
    try:
        from . import qoder_auth
        n = qoder_auth.cleanup_orphan_logins()
        if n:
            try:
                store.log_schedule("info", f"清理了 {n} 个遗留的 Qoder 登录进程", "info")
            except Exception:
                pass
    except Exception:
        pass
    yield


app = FastAPI(title="Personal Model Gateway", version=__version__, lifespan=_lifespan)

# P26：CORS。Chatbox 手机端是 WebView 里跑 ai-sdk（UA `ai-sdk/openai/4.0.60 ...
# runtime/browser`），跨域请求前会先发 OPTIONS 预检。原来没有任何中间件，
# Starlette 对未定义方法一律 405 —— 预检失败，浏览器直接判跨域失败，报 `Load failed`，
# 但 nginx 日志里请求其实已经到了（405 就是证据）。所以不是 HTTPS / TLS 的问题。
#
# 这里放开跨域：网关用 `Authorization: Bearer` 鉴权、不依赖 Cookie，因此
# allow_credentials=False + allow_origins=["*"] 是安全的——`*` 只允许第三方页面
# 读取响应，不会让它们拿到 API Key（key 只存在客户端里）。
# 中间件要盖住路由里抛出的 401，所以必须挂在最外层（add_middleware 后加的在外层）。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    # "*" 时 Starlette 会回显 Access-Control-Request-Headers，authorization /
    # content-type / x-api-key 这些都自动覆盖，不用逐个列。
    allow_headers=["*"],
    expose_headers=["x-request-id"],
    max_age=86400,
)

# P10：上游可切换。默认 mock（本地零副作用）；服务器配 GATEWAY_UPSTREAM=workbuddy
# + GATEWAY_WB_CREDENTIAL 后走真实账号。凭据路径只在服务器环境变量里，不进代码/git。
if settings.upstream == "workbuddy":
    from . import workbuddy_upstream as UPSTREAM  # type: ignore[assignment]
else:
    UPSTREAM = mock_upstream


def _estimate_tokens(payload: dict[str, Any]) -> int:
    """Token 估算——P41 起用中英文分别算（`tokenize` 模块）。

    以前一律 `chars // 4`，对中文低估 4 倍（实测 202 个中文字 → 223 token，
    估算值只有 50）。这个数字现在直接决定「这次请求大概花多少钱」，
    估错就会把用户送去贵模型，所以必须准。
    """
    from .tokenize import estimate_payload

    return estimate_payload(payload)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """后台会请求 favicon，返回 204 而不是 404（避免控制台噪音）。"""
    from fastapi import Response
    return Response(status_code=204)


@app.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    _check_api_key(request)
    # R1：两个对外模型名，客户端用 model 字段选模式。
    # low = 免费优先（没有免费才降级付费）；high = 指定强模型、愿意花积分。
    descriptions = {
        settings.low_model: "低成本：免费优先，没有免费模型时才降级到付费；适合简单任务",
        settings.high_model: "高质量：按配置的强模型优先级调度，账号积分耗尽自动切换；适合复杂任务",
    }
    return {
        "object": "list",
        "data": [
            {
                "id": mid,
                "object": "model",
                "created": 0,
                "owned_by": "personal-gateway",
                "description": descriptions.get(mid, ""),
            }
            for mid in settings.published_models()
        ],
    }


@app.get("/v1/gateway/status")
async def gateway_status(request: Request) -> dict[str, Any]:
    """网关当前状态（P36）：模型、账号、余额、倍率、免费清单、冷却表。

    用途有两个：
    - 客户端挂一个 `gateway_status` 工具，模型要回答「我是什么模型 / 额度多少」
      时自己去取**实时**数据（比事实块更准）；
    - 人直接在浏览器里打开看一眼当前调度到了什么。

    鉴权与其它 /v1 接口一致（API Key）。
    """
    _check_api_key(request)
    try:
        return {"ok": True, **facts.status_payload()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"status failed: {type(exc).__name__}")


def _validate_chat_payload(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        raise HTTPException(status_code=400, detail="messages must be a non-empty array")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise HTTPException(status_code=400, detail="model is required")

    # R1：只接受两个已发布的对外模型名；personal-agent 旧名已确认不保留 → 404。
    if not settings.mode_of(model):
        raise HTTPException(
            status_code=404,
            detail=f"model '{model}' not found; published: {settings.published_models()}",
        )
    return body


@app.post("/v1/chat/completions/control")
async def chat_completions_control(request: Request) -> dict[str, str]:
    """R2.17：WorkBuddy 客户端「停止生成」控制端点。

    客户端把自定义模型 URL 配成 `.../gw/v1/chat/completions`，但停止按钮
    自己拼路径时丢掉了 `/gw` 前缀，直接 POST `/v1/chat/completions/control`，
    线上 nginx 记了几十次 404。真正的取消是客户端断开 SSE 连接完成的，
    这里只做确认应答，避免 404 被 UI 当成「模型出错」。
    """
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    _check_api_key(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    payload = _validate_chat_payload(body)
    stream = bool(payload.get("stream", False))
    # R1：本次模式写入 contextvar，调度与响应回写都读它（每请求独立 task，天然隔离）。
    use_mode(settings.mode_of(payload["model"]) or "low")

    req_id = store.record_request(
        public_model=payload["model"], stream=stream, payload=payload
    )
    started = time.monotonic()
    token_estimate = _estimate_tokens(payload)

    # P3：上下文校验 + 原样透传（不裁剪工具/图片/system；结构错误明确报 400）
    try:
        payload = check_and_passthrough(payload, context_window=settings.context_window)
    except ContextError as e:
        store.finalize_request(
            req_id, status="error", error=f"context:{e}",
            latency_ms=int((time.monotonic() - started) * 1000),
            token_estimate=token_estimate,
        )
        raise HTTPException(status_code=400, detail=str(e))

    # P4：路由决策。检测本次请求的能力需求。
    need_tools = bool(payload.get("tools"))
    need_image = any(
        isinstance(m.get("content"), list)
        and any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
        for m in (payload.get("messages") or [])
        if isinstance(m, dict)
    )
    need_context = _estimate_tokens(payload)

    # P41：事实块档位在**路由之前**就能定（只看用户消息里有没有问模型/额度），
    # 所以「总 token 估算 → 路由排序 → 实际注入」三者完全一致，
    # 不需要迭代，也没有「事实块里的模型名对不上实际用的模型」的问题。
    fact_mode = facts.pick_mode(payload)
    est_tokens = need_context + facts.token_estimate(fact_mode)

    candidates = get_candidates(payload)  # P7 由真实适配器提供；mock 阶段见下方默认实现
    plan = build_plan(
        payload,
        need_tools=need_tools, need_image=need_image, need_context=need_context,
        est_tokens=est_tokens,
    )
    if not plan.candidates:
        store.finalize_request(
            req_id, status="error", error=f"route:{plan.reason}",
            latency_ms=int((time.monotonic() - started) * 1000),
            token_estimate=token_estimate,
        )
        raise HTTPException(status_code=503, detail=plan.reason)
    chosen = plan.candidates[0]
    route_fields = {
        "platform": chosen.platform,
        "model_name": chosen.model_name,
        "account_label": chosen.account_label,
        "choose_reason": plan.reason,
    }
    # R2.22：路由一确定就把实际模型/账号回填到 pending 记录，
    # 让「进行中」的明细行也显示真实模型名而不是 unknown（不必等 finalize）。
    store.attach_route(req_id, **route_fields)
    # R1 提醒（low）：本次选中了付费模型 → 当天首封提醒。
    # R2.52：只有免费池耗尽（free_pairs==0）才发；单次超时回落付费不发。
    if current_mode() == "low" and chosen.est_cost is not None and chosen.est_cost > 0:
        try:
            from . import notify
            exhausted, _fp_status = notify.free_pool_exhausted()
            if exhausted:
                from .scheduler import free_fallback_summary
                notify.note_paid_start(
                model=chosen.model_name, account=chosen.account_label,
                cost=chosen.est_cost, balance=chosen.balance,
                daily_total=store.credit_today(),
                price_factor=chosen.price_factor,
                choose_reason=plan.reason,
                free_alternatives=free_fallback_summary(plan.rejected),
                platform=chosen.platform,
                free_pool_status=_fp_status,
            )
        except Exception:
            pass
    # P19/P35：多账号 —— 把路由选中的账号带进上游调用（流式在生成器内部再设一次，
    # 因为异步生成器可能在另一个上下文里被迭代）。
    account_label = chosen.account_label
    # P44：能用 contextvar 的平台有两个（workbuddy / qoder），按选中的候选定。
    use_ctx = bool(account_label) and chosen.platform in ("workbuddy", "qoder")

    # P36/P41：事实块注入。放在路由之后 —— 事实块要说「当前实际用的模型」，
    # 而那个模型是调度器选出来的，不是后台当前选中的那个。
    # 档位在路由前就定了（fact_mode），所以这里的实际长度与路由时的估算一致。
    try:
        payload = facts.inject(payload, model=chosen.model_name, mode=fact_mode)
    except Exception:
        pass  # 事实注入失败不能连累正常请求

    finalized = {"done": False}

    def finalize(status: str, error: str | None = None,
                 latency_ms: int | None = None, **extra: Any) -> None:
        # 保证任何退出路径恰好落一次记录（否则取消会留下 pending）
        if finalized["done"]:
            return
        finalized["done"] = True
        # R2.17：调用方（流式生成器）显式给出结束时刻算出的耗时；
        # 没给时才退回原算法（非流式/早期错误路径确实就是此刻结束）。
        if latency_ms is None:
            latency_ms = int((time.monotonic() - started) * 1000)
        # P41：真实用量 → 价格曲线观测点。每条成功的请求都产出一组
        # (model, prompt_tokens, credit)，**零额外成本**把模型价格学出来。
        # 探针（gateway/probe.py）退居为冷启动/校正用途。
        if status == "success":
            # R2.17：只记成功请求的耗时作为「慢候选」证据（取消/客户端耐心不算数）
            try:
                from . import latency
                latency.note(route_fields.get("model_name"),
                             route_fields.get("account_label"),
                             latency_ms / 1000.0)
            except Exception:
                pass
            try:
                from . import costmodel

                credit = extra.get("actual_credit")
                ptoks = extra.get("prompt_tokens")
                mid = route_fields.get("model_name")
                if credit is not None and ptoks:
                    costmodel.observe(mid, int(ptoks), float(credit))
            except Exception:
                pass
            # R1 提醒（high）：请求成功后检查该账号余额是否跌破平台百分比阈值。
            # 边沿触发 + 回升重置在 notify 内部；失败不影响响应。
            if current_mode() == "high":
                try:
                    from . import notify
                    notify.check_low_balance(
                        account=route_fields["account_label"],
                        platform=route_fields["platform"],
                        balance=chosen.balance,
                    )
                except Exception:
                    pass
        store.finalize_request(
            req_id,
            status=status,
            error=error,
            latency_ms=latency_ms,
            token_estimate=token_estimate,
            platform=route_fields["platform"],
            model_name=route_fields["model_name"],
            account_label=route_fields["account_label"],
            choose_reason=route_fields["choose_reason"],
            **extra,
        )

    if stream:
        async def event_stream() -> AsyncIterator[str]:
            outcome = "cancelled"  # 默认：中途断开/未正常收尾，绝不记成功
            outcome_error: str | None = "client_disconnected_or_incomplete"
            # R2.17：真实结束时刻（不是生成器被 GC 的时刻）与最后一帧时刻。
            ended_at: float | None = None
            last_event_at = started
            # P30：流式也要记账。原来这条分支只把帧转出去、从不解析 usage，
            # 而 Chatbox / Codex / 手机 App 全是 stream=true，等于最常用的入口
            # 100% 拿不到金额（今天 38 条流式成功，0 条有 credit）。
            actual_credit: float | None = None
            # P41：真实输入 token（上游 usage 给的）。有了它才能把
            # (tokens, credit) 对上号，把模型价格零成本地学出来。
            real_prompt_tokens: int | None = None
            # P35：候选按优先级排好，逐个尝试。**只在还没吐出任何内容时**才能换号
            # —— 已经发出去的半截回答收不回来，换号只会拼出两段不相干的内容。
            cands = list(plan.candidates)
            attempt = 0
            last_error: str | None = None
            error_emitted = False   # 是否已经向客户端吐过 error 帧（避免重复）
            try:
                while attempt < len(cands):
                    cand = cands[attempt]
                    attempt += 1
                    ctx = None
                    if use_ctx:
                        ctx = _use_account_for(cand)
                    produced = False
                    completed = False
                    finish_seen = False   # R2.40：收到非空 finish_reason = 内容已完整
                    # R2.48/51：断流诊断——只记录帧统计，绝不参与成功/失败判定。
                    frame_diag: list[str] = []       # 最后 6 帧标签
                    frame_idx = 0                     # 总帧序号
                    content_chars = 0                 # 全程累计 delta.content 字符数
                    content_frames = 0                # 带实质 content 的帧数
                    last_content_idx = -1             # 最后一个 content 帧的序号
                    first_label = ""                  # 首帧标签
                    try:
                        async for chunk in _stream_upstream(payload, cand):
                            if await request.is_disconnected():
                                ended_at = time.monotonic()
                                outcome, outcome_error = "cancelled", "client_disconnected"
                                break
                            if actual_credit is None:
                                got = credit_from_sse_chunk(chunk)
                                if got is not None:
                                    actual_credit = got
                            if real_prompt_tokens is None:
                                got_tok = prompt_tokens_from_sse_chunk(chunk)
                                if got_tok is not None:
                                    real_prompt_tokens = got_tok
                            # R2.40：记录上游是否明确标记「内容已吐完」。
                            # 断流若发生在 finish_reason 之后，内容其实完整，
                            # 只差收尾帧 —— 不能当成失败弹错误给用户。
                            if chunk_has_finish_reason(chunk):
                                finish_seen = True
                            # R2.48/51：只观测。维护滑动窗口 + 全程 content 统计，断流时落日志定位断点。
                            _lbl = chunk_diag_label(chunk)
                            frame_diag.append(_lbl)
                            if len(frame_diag) > 6:
                                frame_diag.pop(0)
                            if not first_label:
                                first_label = _lbl
                            _clen = chunk_content_len(chunk)
                            if _clen:
                                content_chars += _clen
                                content_frames += 1
                                last_content_idx = frame_idx
                            frame_idx += 1
                            produced = True
                            last_event_at = time.monotonic()
                            yield chunk
                        else:
                            # 迭代器自然耗尽 = 正常收到 [DONE]
                            completed = True
                    except _upstream_error_types() as e:
                        last_error = e.reason
                        ended_at = time.monotonic()
                        # R2.48/51：断流诊断（只记录，不改判据/行为）。落库供事后查断点：
                        # 关键看 content_chars/content_frames + last_content_idx vs total_frames
                        # ——若 content 早已吐完、断流发生在一串空壳帧期间，就是「内容完整但没 stop」。
                        try:
                            store.log_schedule(
                                "stream_diag",
                                f"{cand.account_label}/{cand.model_name} "
                                f"total={frame_idx} finish={finish_seen} "
                                f"usage={actual_credit is not None} ptok={real_prompt_tokens is not None} "
                                f"content_chars={content_chars} content_frames={content_frames} "
                                f"last_content_idx={last_content_idx} "
                                f"first={first_label or '(none)'} "
                                f"tail={'|'.join(frame_diag) or '(none)'} "
                                f"err={e.reason[:60]}",
                                "warn",
                            )
                        except Exception:
                            pass
                        # R2.40：内容已完整吐出后、收尾帧前断流 → 静默补 [DONE] 收尾。
                        # 上游偶发在发完内容+finish_reason 后 TCP reset（差 usage/[DONE]），
                        # 此时客户端已看到完整回答，再吐 error 帧会让它误报「出现错误」。
                        # 只有 finish_reason 明确到达才走这条，吐一半断流仍照常报错。
                        if finish_seen:
                            yield "data: [DONE]\n\n"
                            outcome, outcome_error = "success", None
                            _note_used(cand)
                            store.log_schedule(
                                "stream_recovered",
                                f"{cand.account_label}/{cand.model_name} 内容完整后收尾断流，"
                                f"已静默补 [DONE]（{e.reason[:80]}）",
                                "info",
                            )
                            break
                        if not produced and attempt < len(cands) and _is_retryable(e.reason):
                            # 换号重试：先记住错误，**绝不能在这里吐 error 帧**。
                            # 否则客户端会先收到 error+[DONE]，再收到下一个候选的内容
                            # —— ai-sdk 见到 error 帧就报「自定义模型出现错误」，
                            # 即便后面的候选已经正常答完（用户看到「回复完了还报错」）。
                            # 与非流式路径对齐：失败先攒着，全部候选都失败才统一报错。
                            _cool_down(cand, e.reason)
                            _log_switch(cand, e.reason, attempt, len(cands))
                            continue
                        # 没得换了（或内容已吐出一半不可挽回）→ 立即报错
                        outcome, outcome_error = "error", e.reason
                        try:
                            yield "data: " + json.dumps(
                                {"error": {"message": e.reason, "type": "upstream_error"}},
                                ensure_ascii=False,
                            ) + "\n\n"
                            if not produced:
                                yield "data: [DONE]\n\n"
                            error_emitted = True
                        except Exception:
                            pass
                    except Exception as e:  # noqa: BLE001
                        last_error = err_brief(e)
                        ended_at = time.monotonic()
                        outcome, outcome_error = "error", err_brief(e)
                        try:
                            yield "data: " + json.dumps(
                                {"error": {"message": str(e), "type": "internal_error"}},
                                ensure_ascii=False,
                            ) + "\n\n"
                            if not produced:
                                yield "data: [DONE]\n\n"
                            error_emitted = True
                        except Exception:
                            pass
                    finally:
                        if ctx is not None:
                            _reset_account_for(cand, ctx)
                    if completed:
                        ended_at = time.monotonic()
                        outcome, outcome_error = "success", None
                        _note_used(cand)
                        break
                    if outcome == "cancelled":
                        break
                else:
                    # 所有候选都试完、没有一个成功，且上面每次都走了「换号 continue」
                    # （所以没在任何一处吐过 error 帧）→ 这里统一吐一次，客户端才有明确失败信号。
                    if not error_emitted and last_error:
                        outcome, outcome_error = "error", last_error
                        try:
                            yield "data: " + json.dumps(
                                {"error": {"message": last_error, "type": "upstream_error"}},
                                ensure_ascii=False,
                            ) + "\n\n"
                            yield "data: [DONE]\n\n"
                        except Exception:
                            pass
            except Exception as e:  # noqa: BLE001
                ended_at = time.monotonic()
                outcome, outcome_error = "error", err_brief(e)
                try:
                    yield "data: " + json.dumps(
                        {"error": {"message": str(e), "type": "internal_error"}},
                        ensure_ascii=False,
                    ) + "\n\n"
                    yield "data: [DONE]\n\n"
                except Exception:
                    pass
            finally:
                # 无论正常/异常/生成器被关闭（客户端取消），都落一次终态。
                # R2.17：耗时用上面记录的真实时刻，不用 finally 的执行时刻
                # ——生成器被延迟回收时会把取消请求记成几小时。
                finalize(
                    outcome, outcome_error,
                    latency_ms=stream_latency_ms(started, ended_at, last_event_at),
                    actual_credit=actual_credit,
                    prompt_tokens=real_prompt_tokens,
                )

        # 客户端断开（取消）时也要落记录：用后台任务兜底在该流被 GC 前处理
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：同样的换号重试逻辑
    result = None
    last_error: str | None = None
    cands = list(plan.candidates)
    attempt = 0
    while attempt < len(cands):
        cand = cands[attempt]
        attempt += 1
        ctx = None
        if use_ctx:
            ctx = _use_account_for(cand)
        try:
            result = await _nonstream_upstream(payload, cand)
            last_error = None
            _note_used(cand)
            break
        except _upstream_error_types() as e:
            last_error = e.reason
            if attempt < len(cands) and _is_retryable(e.reason):
                _cool_down(cand, e.reason)
                _log_switch(cand, e.reason, attempt, len(cands))
                continue
            finalize("error", error=e.reason)
            raise HTTPException(status_code=502, detail={"error": e.reason})
        except Exception as e:  # noqa: BLE001
            finalize("error", error=err_brief(e))
            raise HTTPException(status_code=500, detail="internal error")
        finally:
            if ctx is not None:
                _reset_account_for(cand, ctx)

    if result is None:
        finalize("error", error=last_error or "没有可用候选")
        raise HTTPException(status_code=502, detail={"error": last_error or "no candidate"})

    # P10/P30：响应里有 usage.credit 就落实际扣费；没有就保持 NULL（不用估算冒充）。
    # 不再限定 upstream == workbuddy：credit 是 OpenAI 兼容扩展字段，有就取，解析本身无副作用。
    from .credit import credit_from_usage, prompt_tokens_from_usage
    actual_credit = (
        credit_from_usage(result.get("usage")) if isinstance(result, dict) else None
    )
    real_prompt_tokens = (
        prompt_tokens_from_usage(result.get("usage")) if isinstance(result, dict) else None
    )
    finalize("success", actual_credit=actual_credit, prompt_tokens=real_prompt_tokens)
    return JSONResponse(result)


# Responses API（Codex 0.142+ 只支持 wire_api=responses，chat 已被移除）
from .responses import responses_endpoint  # noqa: E402

app.add_api_route("/v1/responses", responses_endpoint, methods=["POST"])

# P33/P34：后台维护线程（同步账号、刷新探针、清理状态）。真实上游才启动。
try:
    from . import background as _background

    _background.start()
except Exception as exc:  # pragma: no cover
    # P49：原来这里是 `except Exception: pass` —— 后台线程起不来时一点痕迹都没有，
    # 表现是「每天到点没邮件、凭据也不续期」，用户完全无从排查。
    try:
        from .store import store

        store.log_schedule(
            "background",
            "后台线程启动失败：%s: %s" % (type(exc).__name__, exc), "error")
    except Exception:
        pass

# 模块末尾挂载后台路由：此时本文件所有符号已定义，admin.py 反向 import 不会循环。
from .admin import admin as _admin_router  # noqa: E402

app.include_router(_admin_router)

# 静态后台页面
from fastapi.responses import FileResponse  # noqa: E402
from .config import PROJECT_ROOT  # noqa: E402


@app.get("/admin/vendor/{asset:path}", include_in_schema=False)
async def admin_vendor(asset: str):
    """后台内联页引用的本地第三方静态资源（R1：ECharts）。

    走独立路由而不是 StaticFiles mount：只白名单 vendor 目录里的文件，
    且只允许单个文件名，避免路径穿越读到仓库其它文件。这些库文件本身
    不含用户数据，登录前可加载（页面里真正的 API 仍走会话鉴权）。
    """
    from pathlib import Path
    if "/" in asset or "\\" in asset or ".." in asset:
        raise HTTPException(status_code=404, detail="not found")
    path = PROJECT_ROOT / "admin" / "vendor" / asset
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    media = "application/javascript" if asset.endswith(".js") else "application/octet-stream"
    return FileResponse(path, media_type=media)


@app.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    """后台首页（P17）：会话有效 → 页面；带对令牌 → 签发会话并跳干净地址；否则登录页。"""
    from .admin_auth import admin_page as render_admin
    return render_admin(request)


@app.post("/admin/login", include_in_schema=False)
async def admin_login(request: Request):
    from .admin_auth import handle_login
    return await handle_login(request)


@app.post("/admin/api/login", include_in_schema=False)
async def admin_api_login(request: Request):
    """页面内输口令换会话（P21）：整页提示里的输入框走这里，返回 JSON。"""
    from .admin_auth import handle_api_login
    return await handle_api_login(request)


@app.post("/admin/logout", include_in_schema=False)
async def admin_logout(request: Request):
    from .admin_auth import handle_logout
    return await handle_logout(request)


@app.post("/admin/magic", include_in_schema=False)
async def admin_magic(request: Request):
    """登录页上的「发一封登录链接到邮箱」（P20）。"""
    from .admin_auth import handle_magic_request
    return await handle_magic_request(request)


@app.get("/admin/copy", include_in_schema=False)
async def admin_copy():
    """邮件里「复制」按钮的落点（P24）：纯前端读 #t= 片段，一键拷口令。"""
    from .admin_auth import copy_page
    return copy_page()
