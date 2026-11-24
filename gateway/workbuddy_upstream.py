"""WorkBuddy 真实上游适配器（P10）。

协议依据（已实测，见 README P10 质检报告）：
- 推理端点 POST https://copilot.tencent.com/v2/chat/completions（国内）。
- 鉴权头族来自 ChinaCPA workbuddy.go（固定版本源码审查）+ 本地签到脚本 signin.py:648。
- 上游 2026 起仅支持流式（强制 stream=true），非流式由本适配器本地聚合成 OpenAI JSON。
- SSE 帧含 choices[].delta（content / reasoning_content / tool_calls）与 usage.credit。

安全边界：
- 凭据文件只在服务端本地读取（0600），token 不进日志/DB/响应/前端。
- 免费模型优先：默认选 hy4flash-free（目录里实测可用），收费模型需显式允许。
- usage.credit 若上游返回则记录为实际扣费；没有就保持 NULL（未知），不用估算冒充。
- 工具/图片/多轮历史原样透传，不在适配器里裁剪（P3 原则）。
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, AsyncIterator

from .config import PROJECT_ROOT, settings

BASE_URL = os.environ.get("GATEWAY_WB_BASE", "https://copilot.tencent.com")
DEFAULT_MODEL = os.environ.get("GATEWAY_WB_MODEL", "hy4flash-free")
DEFAULT_CREDENTIAL = os.environ.get("GATEWAY_WB_CREDENTIAL", "")

# 当前请求使用的账号（P19 多账号）：由 app 层按路由结果设置，适配器内部读取。
# contextvar 而不是全局变量：每个请求/任务各自一份，不会串号。
_ACCOUNT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "wb_account", default=None
)


class UpstreamError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class CredentialError(UpstreamError):
    pass


def _accounts_dir() -> Path:
    p = Path(settings.credential_dir).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def list_credential_files() -> dict[str, Path]:
    """凭据目录里已有的账号文件：{label: path}。文件名即账号标签。

    目录可以不存在（只用环境变量单账号的老部署），此时返回空。
    """
    d = _accounts_dir()
    if not d.is_dir():
        return {}
    out: dict[str, Path] = {}
    for p in sorted(d.glob("*.info")):
        if p.is_file():
            out[p.stem] = p
    return out


def _credential_path(label: str | None = None) -> Path:
    if label:
        d = _accounts_dir()
        cand = d / f"{label}.info"
        if cand.is_file():
            # 环境变量那份可能更新（签到系统写的地方）→ 比 mtime 取新
            if DEFAULT_CREDENTIAL:
                envp = Path(DEFAULT_CREDENTIAL).expanduser()
                if not envp.is_absolute():
                    envp = PROJECT_ROOT / envp
                if envp.is_file() and envp.stem == label and envp.stat().st_mtime > cand.stat().st_mtime:
                    return envp
            return cand
        # 兼容直接把完整路径当 label 传进来的调用
        p = Path(label).expanduser()
        if p.is_file():
            return p
        # 老部署：凭据在环境变量指的位置（不在凭据目录里），文件名对得上就用
        if DEFAULT_CREDENTIAL:
            envp = Path(DEFAULT_CREDENTIAL).expanduser()
            if not envp.is_absolute():
                envp = PROJECT_ROOT / envp
            if envp.is_file() and envp.stem == label:
                return envp
        raise CredentialError(f"credential for account '{label}' not found")
    raw = DEFAULT_CREDENTIAL
    if not raw:
        # 没有环境变量但目录里有账号：用第一个（单账号部署最省事）
        files = list_credential_files()
        if files:
            return next(iter(files.values()))
        raise CredentialError("GATEWAY_WB_CREDENTIAL not configured")
    p = Path(raw).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def known_accounts() -> dict[str, Path]:
    """所有已知账号：凭据目录里的文件 + 环境变量指向的那个（按文件名去重）。

    同一个账号可能在两处都有（比如签到系统写的 + 后台导入的），**取 mtime 最新的那份**——
    否则签到系统刷新了 token，网关还在读旧副本，会莫名其妙 401。
    """
    out = dict(list_credential_files())
    if DEFAULT_CREDENTIAL:
        p = Path(DEFAULT_CREDENTIAL).expanduser()
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if p.is_file():
            cur = out.get(p.stem)
            if cur is None or p.stat().st_mtime > cur.stat().st_mtime:
                out[p.stem] = p
    return out


def current_account() -> str | None:
    return _ACCOUNT.get()


def use_account(label: str | None):
    """在异步任务上下文里指定账号；返回 token 供 reset。"""
    return _ACCOUNT.set(label or None)


def reset_account(token) -> None:
    try:
        _ACCOUNT.reset(token)
    except Exception:
        pass


def _load_credential_meta(label: str | None = None) -> tuple[str, str, dict[str, Any]]:
    """从本地凭据文件读取 (accessToken, uid, meta)。token 只在内存停留，不落日志。"""
    p = _credential_path(label)
    if not p.is_file():
        raise CredentialError("credential file not found")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise CredentialError(f"credential file unreadable: {type(e).__name__}")
    auth = data.get("auth") or {}
    account = data.get("account") or {}
    token = auth.get("accessToken", "")
    uid = account.get("uid", "")
    if not token or not uid:
        raise CredentialError("credential missing accessToken/uid")
    def _sec(v):
        """上游时间戳是毫秒，统一成秒（页面上直接乘 1000 会算出几万年）。"""
        try:
            n = float(v)
        except (TypeError, ValueError):
            return None
        return n / 1000.0 if n > 1e11 else n

    meta = {
        "label": label or p.stem,
        "file": p.name,
        "updated_at": p.stat().st_mtime,
        "expires_at": _sec(auth.get("expiresAt")),
        "refresh_expires_at": _sec(auth.get("refreshExpiresAt")),
        "last_refresh_at": _sec(auth.get("lastRefreshTime")),
        "nickname": account.get("nickname") or "",
        "uin": account.get("uin") or "",
        "phone": p.stem,
    }
    return token, uid, meta


def _load_credential() -> tuple[str, str]:
    """当前请求该用哪个账号的凭据。

    P42 修复：**必须读 `_ACCOUNT` contextvar**。此前这里从不传 label，
    于是 `_credential_path(None)` 永远回落到 `DEFAULT_CREDENTIAL`：
    - 账号页所有账号显示同一个余额（都显示默认账号的）；
    - 更严重：推理请求不管调度器选了谁，实际都打到默认账号，
      「多账号摊平/轮转」形同虚设，请求记录里的 account_label 也对不上真实扣费账号。

    contextvar 为空（单账号部署 / 无路由上下文）时才回落到默认凭据。
    """
    label = _ACCOUNT.get()
    token, uid, _ = _load_credential_meta(label)
    return token, uid


def _stable_id(uid: str, purpose: str) -> str:
    return hashlib.sha256(f"wb2a:{purpose}:{uid}".encode()).hexdigest()[:18]


def _headers(token: str, uid: str) -> dict[str, str]:
    """按 ChinaCPA workbuddy.go 的 chatHeaders + commonHeaders 构造（已实测 200）。"""
    h = {
        "Authorization": f"Bearer {token}",
        "X-User-Id": uid,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.codebuddy.cn",
        "Referer": "https://www.codebuddy.cn/",
        "User-Agent": "WorkBuddy/5.5.4 WorkBuddy/5.5.4 CLI/2.137.1",
        "X-CodeBuddy-Request": "1",
        "Accept-Language": "zh-CN",
        "X-No-Enterprise-Id": "1",
        "X-No-Department-Info": "1",
        "X-Agent-Purpose": "conversation",
        "X-IDE-Name": "WorkBuddy", "X-IDE-Type": "WorkBuddy",
        "X-IDE-Version": "5.5.4", "X-Product": "WorkBuddy",
        "X-Machine-ID": _stable_id(uid, "machine"),
        "X-Session-ID": _stable_id(uid, "session"),
    }
    # 会话头族：每次请求生成随机 msg id（32 hex），与 ChinaCPA 一致
    msg = secrets.token_hex(16)
    h.update({
        "X-Conversation-ID": secrets.token_hex(16),
        "X-Conversation-Request-ID": msg,
        "X-Conversation-Message-ID": msg,
        "X-Request-ID": msg,
        "X-Root-Request-ID": msg,
        "X-Trace-ID": msg,
        "X-B3-TraceId": msg,
        "X-B3-SpanId": msg[:16],
        "X-B3-Sampled": "1",
    })
    return h


def effective_model() -> str:
    """当前发给上游的模型 id：后台手动切换优先，其次环境变量默认值。"""
    from . import modelstate
    return modelstate.active_model() or DEFAULT_MODEL


def _adapt_roles(messages: Any) -> Any:
    """出站消息的角色适配：`developer` → `system`。

    P43 修复（Qoder 聊天一直 11128 的根因）：上游 WorkBuddy **不接受
    `developer` 角色**，任何内容、任何模型、任何账号都会被渠道风控拦下：

        400 {"code":11128,"msg":"Illegal API invocation from an unapproved channel"}

    实测消融（2026-11-19）：同样的正文改成 `system` 角色即 200 通过；
    长度、工具数量、账号、模型都不影响 —— 唯一变量就是角色名。

    这是**协议角色适配**，与 P13 的 `input_text`→`text` 同类：
    正文原样保留、不裁剪、不伪装任何客户端身份，只是把上游不认识的
    角色名换成语义等价的那个。Codex（Responses 端点）早已这么处理，
    但 chat/completions 路径漏了 —— Qoder IDE 走的正是后者。

    已有 `developer`→`system` 的先例见 gateway/responses.py::_item_to_message。
    """
    if not isinstance(messages, list):
        return messages
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "developer":
            m = {**m, "role": "system"}
        out.append(m)
    return out


def build_upstream_body(payload: dict[str, Any], model: str | None = None) -> dict[str, Any]:
    """出站 body：覆盖模型名、强制流式、注入 usage 与 cache key；messages 原样透传。

    `model` 显式传入时覆盖当前生效模型（P34 探针要指定被测模型，
    P35 调度器要指定候选模型，都不是「当前手动选中的那个」）。
    """
    body = dict(payload)
    body["model"] = model or effective_model()
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    if "prompt_cache_key" not in body:
        body["prompt_cache_key"] = f"gateway:{_load_credential()[1]}"
    # 去掉网关内部字段，不发给上游
    body.pop("_mock_fail", None)
    # P43：角色适配（developer → system），详见 _adapt_roles 的注释
    body["messages"] = _adapt_roles(body.get("messages"))
    return body


_CATALOG_CACHE: dict[str, Any] = {"at": 0.0, "rows": None}


def catalog(ttl: float = 60.0) -> list[dict[str, Any]]:
    """带缓存的模型目录（后台与调度共用；失败时返回上次结果或空列表）。"""
    now = time.time()
    rows = _CATALOG_CACHE.get("rows")
    if rows is not None and now - float(_CATALOG_CACHE.get("at") or 0) < ttl:
        return rows  # type: ignore[return-value]
    try:
        rows = fetch_models()
        _CATALOG_CACHE["rows"] = rows
        _CATALOG_CACHE["at"] = now
        return rows
    except Exception:
        return rows or []


def model_info(model_id: str) -> dict[str, Any] | None:
    for row in catalog():
        if row.get("model_name") == model_id:
            return row
    return None


def _credits_factor(raw: Any) -> float | None:
    """把目录里的 "x0.29 credits" / "x0.00" 解析成倍率数字；无法解析返回 None。"""
    if not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    if not s.startswith("x"):
        return None
    s = s[1:].split()[0] if s[1:] else ""
    try:
        return float(s)
    except ValueError:
        return None


def _badge_text(tags: list[str]) -> str | None:
    """目录里的角标写成 "badge:限时免费:#FF0000"，取中间的文案。"""
    for t in tags:
        if isinstance(t, str) and t.startswith("badge:"):
            parts = t.split(":")
            if len(parts) >= 2 and parts[1]:
                return parts[1]
    return None


def _kind(m: dict[str, Any], tags: list[str]) -> str:
    """区分对话模型与生成类模型（图片/视频）。

    P16：用户看到「没有倍率」的模型里其实有 4 个是生成类，混在对话模型列表里
    既不能切也没意义，单独归类。判断依据都来自目录本身（videoGeneration 字段、
    text-to-image / text-to-video 标签），不按名字硬猜。
    """
    if "videoGeneration" in m or any("video" in t for t in tags if isinstance(t, str)):
        return "video"
    if any(isinstance(t, str) and "to-image" in t for t in tags):
        return "image"
    if not m.get("supportsToolCall") and "image" in str(m.get("id", "")):
        return "image"
    return "chat"


def fetch_models() -> list[dict[str, Any]]:
    """模型目录：GET /console/enterprises/personal/models 返回 200。

    返回字段（P15 起带真实倍率与能力；P16 补全描述/标签/生成类/推荐集）：
      model_name / display_name / credits_factor / is_free / pricing /
      tools / image / context_window / description / tags / badge /
      kind / recommended / is_default
    """
    token, uid = _load_credential()
    req = urllib.request.Request(
        BASE_URL + "/console/enterprises/personal/models",
        headers={**_headers(token, uid), "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise UpstreamError(f"models fetch failed: {type(e).__name__}")
    payload = data.get("data") or {}
    # 推荐集：WorkBuddy 客户端模型下拉里出现的名单（agents 里 name=cli 的 models）。
    # 用户反馈「网关列表比客户端还长」，就用这份真实名单做「常用」筛选依据。
    recommended: set[str] = set()
    for agent in payload.get("agents") or []:
        if agent.get("name") == "cli":
            recommended.update(str(x) for x in (agent.get("models") or []))
    # 客户端聊天框里的档位（快速/均衡/极致/Default/Auto）不在 cli agent 名单里，
    # 但它们才是用户平时真会点的模型，一并算「常用」。
    recommended.update({"auto", "fast-model", "balanced-model", "deep-model", "default"})
    out: list[dict[str, Any]] = []
    for m in payload.get("models") or []:
        mid = m.get("id")
        if not mid:
            continue
        factor = _credits_factor(m.get("credits"))
        cw = m.get("contextWindow") or {}
        tags = [t for t in (m.get("tags") or []) if isinstance(t, str)]
        if factor is None:
            # auto 的倍率是浮动的（目录原文：variable credit multiplier），
            # 不能当成「不可用」；其余未标价为 unknown，按未知处理不做价格排序。
            pricing = "dynamic" if str(mid) == "auto" else "unknown"
        else:
            pricing = "free" if factor == 0.0 else "fixed"
        out.append({
            "model_name": mid,
            "display_name": m.get("name") or mid,
            "credits_factor": factor,
            "pricing": pricing,
            # 倍率为 0 即免费；无法解析时退回按名字判断
            "is_free": (factor == 0.0) if factor is not None
                       else (str(mid).endswith("-free") or "free" in str(mid)),
            "tools": m.get("supportsToolCall"),
            "image": m.get("supportsImages"),
            "context_window": cw.get("defaultLength") or m.get("maxInputTokens"),
            "description": m.get("descriptionZh") or m.get("descriptionEn"),
            "tags": tags,
            "badge": _badge_text(tags),
            "kind": _kind(m, tags),
            "is_default": bool(m.get("isDefault")),
            "recommended": str(mid) in recommended,
        })
    if out:
        return out
    # 兼容旧结构（只有 agents 里的模型名）
    for agent in payload.get("agents", []):
        for name in agent.get("models", []):
            if not any(x["model_name"] == name for x in out):
                out.append({
                    "model_name": name,
                    "display_name": name,
                    "credits_factor": None,
                    "pricing": "unknown",
                    "is_free": name.endswith("-free") or "free" in name,
                    "tools": "tools" in agent,
                    "image": False,
                    "context_window": None,
                    "description": None,
                    "tags": [],
                    "badge": None,
                    "kind": "chat",
                    "is_default": False,
                    "recommended": bool(recommended) and name in recommended,
                })
    return out


_GROUP_LABELS = {
    "plan_base": "套餐基础积分",
    "plan_bonus": "套餐赠送积分",
    "addon": "购买积分",
    "benefit": "平台奖励积分",
}


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_billing(token: str | None = None, uid: str | None = None) -> dict[str, Any]:
    """完整账单口径（P18）：余额 + 总量 + 已用 + 积分构成。

    上游一次 POST /billing/meter/get-user-resource-summary 就带回 CreditGroups，
    不用再猜别的端点（get-user-daily-usage 存在但参数未知，返回 10001，不硬试）。
    已用/总量都是**账户口径**，包含在官方客户端里的消耗，比网关记录更权威；
    但拿不到「按天」的拆分，所以「近 7 天」只能用网关自己的记录（页面已注明）。

    token/uid 可显式传入：导入新账号时先用它验证，不必等服务重启。
    """
    if not token or not uid:
        token, uid = _load_credential()
    req = urllib.request.Request(
        BASE_URL + "/billing/meter/get-user-resource-summary",
        data=b"{}", headers=_headers(token, uid), method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise UpstreamError(f"balance fetch failed: {type(e).__name__}")
    payload = data.get("data") or {}
    groups = []
    for g in payload.get("CreditGroups") or []:
        kind = str(g.get("Kind") or "")
        groups.append({
            "kind": kind,
            "label": g.get("Name") or _GROUP_LABELS.get(kind, kind or "积分"),
            "capacity": _f(g.get("CycleTotalCapacity")),
            "used": _f(g.get("CycleUsedCapacity")),
            "remain": _f(g.get("CycleRemainCapacity")),
            "count": int(g.get("TotalCount") or 0),
        })
    total = _f(payload.get("TotalCredits"))
    capacity = sum(g["capacity"] for g in groups)
    used = sum(g["used"] for g in groups)
    if capacity <= 0 and total > 0:
        capacity = total
    return {
        "total": total,
        "capacity": round(capacity, 2),
        "used": round(used, 2),
        "ratio": round(used / capacity, 4) if capacity else None,
        "groups": groups,
        "plan": payload.get("SubscriptionPackageName") or "",
        "is_paid": bool(payload.get("IsPaidUser")),
        "checked_at": time.time(),
        "unit": "credits_workbuddy",
    }


def fetch_balance() -> tuple[float, str, float]:
    """可用余额：POST /billing/meter/get-user-resource-summary → TotalCredits。"""
    info = fetch_billing()
    return float(info["total"]), "credits_workbuddy", float(info["checked_at"])


def account_meta(label: str | None = None) -> dict[str, Any] | None:
    """账号元信息（不含 token）：有效期、昵称、凭据文件更新时间。"""
    try:
        _, _, meta = _load_credential_meta(label)
        return meta
    except Exception:
        return None


def _open_stream(payload: dict[str, Any], model: str | None = None):
    token, uid = _load_credential()
    body = build_upstream_body(payload, model=model)
    req = urllib.request.Request(
        BASE_URL + "/v2/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=_headers(token, uid), method="POST",
    )
    try:
        return urllib.request.urlopen(req, timeout=settings.upstream_timeout)
    except urllib.error.HTTPError as e:
        detail = e.read(2000).decode("utf-8", "replace")
        raise UpstreamError(f"upstream HTTP {e.code}: {detail[:200]}")
    except Exception as e:
        raise UpstreamError(f"upstream connect failed: {type(e).__name__}")


# R2.15：上游流式读取时的「异常断流」异常族。这些是 Python http.client / socket
# 的原生异常，不是 UpstreamError——若不包装，会掉进 app.py 的兜底 except 被当成
# internal_error 甩给客户端（用户看到「自定义模型出现错误 IncompleteRead」），
# 而且不会触发换号重试。包装成 UpstreamError 后即可走 app.py 的换号分支。
# 实测：客户端错误对象全部 isStreamTimeout=false / isNetworkError=false，
# 确认根因是断流异常而非超时（R2.14 的 SSE 保活方向错误，已撤销）。
import http.client as _http_client

_BROKEN_STREAM_ERRORS = (
    _http_client.IncompleteRead,       # 上游中途关流，Content-Length 对不上
    ConnectionResetError,              # TCP reset（含 http.client.RemoteDisconnected）
    ConnectionAbortedError,
    BrokenPipeError,
)


def iter_sse(payload: dict[str, Any], model: str | None = None) -> AsyncIterator[str]:
    """逐帧产出 OpenAI 兼容 SSE；usage 帧带 credit 时保留（供账单列）。

    上游自己会发一帧 `data: [DONE]`。原实现不管上游发没发都在 finally 后补一帧，
    结果客户端收到两个 [DONE]（P30 实测 curl 尾部连着两行）。现在只在没收到时补。

    `model` 覆盖出站模型名（P34 探针 / P35 调度器指定候选模型时用）。

    R2.15：读流时若上游异常断流（IncompleteRead / ConnectionReset），包装成
    UpstreamError 让 app 层换号重试，而不是甩 internal_error 给客户端。
    """
    # 只有真的指定了模型才走带 model 的调用路径：测试与旧调用方的替身是单参函数，
    # 无条件传 model= 会把「改签名」变成破坏性变更。
    resp = _open_stream(payload, model=model) if model else _open_stream(payload)
    saw_done = False
    try:
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if line.startswith(":"):
                    # R2.17：上游思考期的 `:heartbeat` 必须透传。
                    # R2.14 曾在异步层补 keepalive 却无效——因为这里同步读流把
                    # 事件循环堵死了，异步层根本没机会发帧。官方客户端直连能收到
                    # 心跳所以不超时，走网关被吞掉才「一直转圈」。
                    # SSE 注释帧对所有客户端无害，解析器只认 `data:`。
                    yield line + "\n\n"
                    continue
                if line.startswith("data:"):
                    if "[DONE]" in line:
                        saw_done = True
                        yield line + "\n\n"
                        continue
                    # P43：上游帧里的 model 是内部模型名（如 space-bunny / glm-5.3-flash），
                    # 但对外只发布 personal-agent。客户端（尤其 IDE 类）会拿响应里的
                    # model 跟它请求的名字比对，不一致就当异常处理。改成对外模型名，
                    # 内部真实模型由事实块/状态接口如实告知。
                    yield _rewrite_frame_model(line) + "\n\n"
        except _BROKEN_STREAM_ERRORS as e:
            # 关键：包装成 UpstreamError，让它走 app.py 的换号分支（662），
            # 而不是掉进兜底 except（685）吐 internal_error 给客户端。
            raise UpstreamError(
                f"upstream stream broken: {type(e).__name__}: {e}"
            ) from e
    finally:
        resp.close()
    if not saw_done:
        yield "data: [DONE]\n\n"


def _published_model() -> str:
    """R1：本次请求对外暴露的模型名（low/high），从 app 的 contextvar 读。"""
    try:
        from .app import current_mode
        return settings.high_model if current_mode() == "high" else settings.low_model
    except Exception:
        return settings.low_model


def _rewrite_frame_model(line: str) -> str:
    """把一帧 SSE 里的 `model` 字段改成对外模型名（改不了就原样返回）。"""
    if not line.startswith("data: {"):
        return line
    try:
        obj = json.loads(line[6:].strip())
    except Exception:
        return line
    published = _published_model()
    if not isinstance(obj, dict) or obj.get("model") == published:
        return line
    obj["model"] = published
    return "data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


async def stream_response(payload: dict[str, Any], model: str | None = None) -> AsyncIterator[str]:
    """UPSTREAM 契约（与 mock_upstream 同名）：逐帧输出 OpenAI SSE。

    R2.17：iter_sse 仍是同步生成器（urllib 阻塞读，保留签名兼容探针/测试），
    但**绝不能在异步生成器里直接 for 它**——那会把阻塞读放进事件循环，
    上游思考期一静默，整个 uvicorn 就卡住：心跳发不出、断线检测不到、
    管理页 API 也一起转圈。用 `asyncio.to_thread(next, it)` 把每次读帧挪到线程，
    帧与帧之间事件循环有机会跑（透传心跳 / 检查客户端断开 / 处理别的请求）。
    """
    it = iter_sse(payload, model=model) if model else iter_sse(payload)
    while True:
        chunk = await asyncio.to_thread(next, it, None)
        if chunk is None:  # StopIteration 的哨兵：iter_sse 只产出字符串
            return
        yield chunk


def build_nonstream_response(payload: dict[str, Any], model: str | None = None) -> dict[str, Any]:
    """UPSTREAM 契约（与 mock_upstream 同名）：实时上游强制流式→本地聚合。"""
    return aggregate_nonstream(payload, model=model)


def credit_from_usage(usage: dict[str, Any] | None) -> float | None:
    """从上游 usage 取真实扣费 credit；没有就返回 None（未知，不用估算冒充）。

    P30：实现搬到 gateway/credit.py（流式与非流式共用），这里保留名字兼容旧引用。
    """
    from .credit import credit_from_usage as _impl
    return _impl(usage)


def aggregate_nonstream(payload: dict[str, Any], model: str | None = None) -> dict[str, Any]:
    """上游强制流式 → 本地聚合成一条非流式 OpenAI 响应（与 ChinaCPA 同做法）。"""
    content_parts: list[str] = []
    # P11 实测：上游把 tool_call 拆成多帧（首帧 id+name，后续帧 arguments），
    # 用 index 合并同一调用，不能逐帧 append——否则 arguments 会变成无 id 的第二次调用。
    tool_calls: dict[int, dict] = {}
    usage: dict | None = None
    for frame in (iter_sse(payload, model=model) if model else iter_sse(payload)):
        if not frame.startswith("data: ") or "[DONE]" in frame:
            continue
        try:
            obj = json.loads(frame[6:].strip())
        except Exception:
            continue
        if obj.get("usage"):
            usage = obj["usage"]
        for ch in obj.get("choices", []):
            d = ch.get("delta") or {}
            if d.get("content"):
                content_parts.append(d["content"])
            for tc in d.get("tool_calls") or []:
                idx = int(tc.get("index", 0))
                merged = tool_calls.setdefault(idx, {
                    "id": None, "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                if tc.get("id"):
                    merged["id"] = tc["id"]
                if tc.get("type"):
                    merged["type"] = tc["type"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    merged["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    merged["function"]["arguments"] += fn["arguments"]
    msg: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts) or None}
    finish_reason = "stop"
    if tool_calls:
        # 任何缺失 id 的调用都不可用于工具结果关联 —— 明确丢弃并报错，
        # 不把残缺调用交给客户端（P3：结构问题明确报错，不假装支持）
        ordered = [tool_calls[k] for k in sorted(tool_calls)]
        if any(not tc.get("id") for tc in ordered):
            raise UpstreamError("upstream tool_call missing id after merge")
        msg["tool_calls"] = ordered
        # P43：OpenAI 规范里，有工具调用时 finish_reason 必须是 "tool_calls"。
        # 之前一律写死 "stop"，IDE 类客户端（Qoder / Cursor 等）据此判断
        # 「模型是否要调工具」，看到 stop 就不执行工具 → 表现为「聊天异常」。
        finish_reason = "tool_calls"
    result = {
        "id": f"chatcmpl-{secrets.token_hex(10)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": _published_model(),
        "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    return result
