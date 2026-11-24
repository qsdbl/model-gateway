"""管理后台鉴权：登录令牌 → 短会话 → 滑动续期（P9 起步，P17 会话化）。

为什么改（用户反馈「页面直接显示密文」）：P9 照抄火烧云设置页，把令牌放在 URL
`?token=` 里，地址栏、浏览器历史、剪贴板、聊天记录里到处都是明文。P17 改成：

1) **令牌只做一次登录**：`/admin?token=` 或登录框提交后，服务端立刻签发会话 cookie，
   并 302 到「去掉 token」的干净地址。之后地址栏只有 `/admin`。
2) **会话可撤销**：sid 存 SQLite（不存令牌），cookie 值是 `sid.HMAC(sid)`；改密码/退出
   登录只需把行标记 revoked，正在用的 cookie 立即失效。
3) **滑动续期**：每次访问把 expires_at 往后推（默认 12h 不活跃即失效，最长 30 天），
   所以手机端连续用不会掉登录，放一夜不动就要重新登录 —— 不用手动「续期」。
4) **失败限流**：同一 IP 在窗口内失败 ≥10 次 → 临时封禁（默认 24h），封禁期间即使
   拿对令牌也……**放行**（有对令牌的人本来就该进，这不削弱防爆破）。解封有后台按钮。
5) **全程审计**：成功/失败/解封都落 `admin_auth_events`（IP、UA、路径、结果、原因），
   页面「安全」标签可查；日志里永远不出现令牌本身或前缀。
6) **邮件找回（P20）**：会话过期/换了设备进不去时，登录页上点一下「发到邮箱」，
   服务端把一次性登录链接发到「安全」页配置的邮箱；点开即登录，不用记口令。

原有能力保留：`Authorization: Bearer <令牌>` 仍可用于脚本/curl 调 API；
未配置 GATEWAY_ADMIN_TOKEN 时整站 503（fail-safe，防公网裸奔）。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .config import settings
from .store import store

SESSION_COOKIE = "mgw_sess"
LEGACY_COOKIE = "mgw_admin_token"   # P9 遗留，只读不写（老链接里的 cookie 直接忽略）

_SID_RE = re.compile(r"^[0-9a-f]{32}$")


# ---------------------------------------------------------------- 基础工具
def admin_token_enabled() -> bool:
    return bool(settings.admin_token)


def client_ip(request: Request) -> str:
    """真实客户端 IP。

    nginx 反代后 socket peer 永远是 127.0.0.1，必须看 X-Real-IP / X-Forwarded-For；
    但**只在**对端是本机时才信这两个头（否则任何人都能伪造 IP 绕过限流）。
    """
    peer = (request.client.host if request.client else "") or ""
    if peer in ("127.0.0.1", "::1", ""):
        xr = (request.headers.get("x-real-ip") or "").strip()
        if xr:
            return xr
        xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if xff:
            return xff
    return peer or "unknown"


def user_agent(request: Request) -> str:
    return (request.headers.get("user-agent") or "").strip()


def _secret() -> bytes:
    """会话签名密钥：首次启动随机生成并落盘（0600），重启后签名仍然有效。

    与 GATEWAY_ADMIN_TOKEN 拼在一起作为密钥，改令牌即等于让所有旧会话失效。
    """
    p = Path(settings.data_dir) / ".session_secret"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(secrets.token_bytes(32))
        os.chmod(p, 0o600)
    try:
        raw = p.read_bytes().strip()
    except Exception:
        raw = b""
    if len(raw) < 16:
        raw = secrets.token_bytes(32)
        p.write_bytes(raw)
        os.chmod(p, 0o600)
    return raw + settings.admin_token.encode()


def _sign(sid: str) -> str:
    return hmac.new(_secret(), sid.encode(), hashlib.sha256).hexdigest()[:32]


def make_cookie_value(sid: str) -> str:
    return f"{sid}.{_sign(sid)}"


def parse_cookie_value(value: str | None) -> str | None:
    """校验 cookie 的签名，返回 sid；伪造/篡改返回 None。"""
    if not value or "." not in value:
        return None
    sid, sig = value.rsplit(".", 1)
    if not _SID_RE.match(sid):
        return None
    if not hmac.compare_digest(sig, _sign(sid)):
        return None
    return sid


def prefix_of(request: Request) -> str:
    """子路径前缀（nginx /gw/ 反代时是 /gw），用于生成跳转地址与 cookie Path。"""
    return (request.headers.get("x-forwarded-prefix") or "").rstrip("/")


def cookie_path(request: Request) -> str:
    return prefix_of(request) or "/"


def _is_https(request: Request) -> bool:
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
    return proto == "https"


# ---------------------------------------------------------------- 限流
def _since_window() -> float:
    return time.time() - settings.admin_fail_window


def block_until(ip: str) -> float | None:
    """该 IP 是否被临时封禁；返回解除时间（epoch），未封禁返回 None。

    规则：窗口内失败次数 ≥ 上限 → 从「最近一次失败」起封禁 block_ttl。
    手动解封写入 result='unban' 事件，只统计解封之后的失败。
    """
    since = _since_window()
    unban = store.last_event_at(ip, "unban", since)
    if unban:
        since = max(since, unban)
    n = store.count_failures(ip, since)
    if n < settings.admin_fail_limit:
        return None
    last = store.last_event_at(ip, "deny", since) or time.time()
    until = last + settings.admin_block_ttl
    return until if until > time.time() else None


def _record(request: Request, result: str, reason: str = "") -> None:
    try:
        store.record_auth_event(
            ip=client_ip(request), ua=user_agent(request),
            path=str(request.url.path), result=result, reason=reason,
        )
    except Exception:
        pass  # 审计失败不能把登录挡死


# ---------------------------------------------------------------- 会话
def session_ttl() -> int:
    return max(60, int(settings.admin_session_ttl))


def current_session(request: Request) -> dict[str, Any] | None:
    """读取并校验会话；有效则滑动续期后返回会话行。"""
    raw = request.cookies.get(SESSION_COOKIE)
    sid = parse_cookie_value(raw)
    if not sid:
        return None
    row = store.get_session(sid)
    if not row or row.get("revoked"):
        return None
    now = time.time()
    if float(row["expires_at"]) <= now:
        return None
    # 绝对上限：从创建起超过 admin_session_max 也失效（防止会话永久存活）
    if settings.admin_session_max and now - float(row["created_at"]) > settings.admin_session_max:
        store.revoke_session(sid)
        return None
    new_exp = now + session_ttl()
    store.touch_session(sid, expires_at=new_exp)
    row["expires_at"] = new_exp
    return row


def create_session(request: Request) -> tuple[str, dict[str, Any]]:
    store.purge_sessions()
    sid = secrets.token_hex(16)
    store.create_session(
        sid, ip=client_ip(request), ua=user_agent(request), ttl=session_ttl()
    )
    return make_cookie_value(sid), store.get_session(sid) or {}


def set_session_cookie(response: Response, request: Request, value: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=session_ttl(),
        path=cookie_path(request),
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(SESSION_COOKIE, path=cookie_path(request))
    response.delete_cookie(LEGACY_COOKIE, path=cookie_path(request))


def revoke_current(request: Request) -> None:
    sid = parse_cookie_value(request.cookies.get(SESSION_COOKIE))
    if sid:
        store.revoke_session(sid)


# ---------------------------------------------------------------- 邮件找回（P20）
EMAIL_KEY = "admin_email"          # 收件邮箱存在 settings 表，后台「安全」页可改


def absolute_base(request: Request) -> str:
    """当前访问的全量根地址（含 https 与 /gw 前缀），用于拼邮件里的可点链接。

    P22：只要配好/探测到固定 HTTPS 域名就优先用它——邮件里的 http 链接会被客户端
    标成不安全，手机点开也走明文；一次性登录链接本身是凭据，更不该走 http。
    """
    try:
        from .tunnel import fixed_https_base

        fixed = fixed_https_base()
    except Exception:
        fixed = ""
    if fixed:
        return fixed
    host = request.headers.get("host") or str(request.base_url).split("//")[-1].strip("/")
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http")
    proto = proto.split(",")[0].strip().lower() or "http"
    return f"{proto}://{host}{prefix_of(request)}"


def magic_recipient() -> str:
    """收件邮箱：后台配置优先，其次环境变量默认值。"""
    return (store.get_setting(EMAIL_KEY) or settings.admin_email or "").strip()


def mask_email(addr: str) -> str:
    """a***@qq.com —— 页面上只显示掩码，避免截图/录屏泄露完整邮箱。"""
    addr = (addr or "").strip()
    if "@" not in addr:
        return addr or "(未配置)"
    name, domain = addr.split("@", 1)
    if len(name) <= 2:
        return f"{name[:1]}***@{domain}"
    return f"{name[:1]}***{name[-1:]}@{domain}"


def magic_ready() -> tuple[bool, str]:
    """能不能发信：邮箱 + SMTP 都得有，缺哪样说哪样。"""
    from .mailer import mail_configured

    if not magic_recipient():
        return False, "还没设置收件邮箱"
    if not mail_configured():
        return False, "服务器还没配置 SMTP"
    return True, ""


def copy_link_for(request: Request, token: str) -> str:
    """「复制口令」页的地址。

    口令放在 URL 的 `#` 之后：片段不会被浏览器发给服务器，所以 nginx access log、
    反向代理日志、Referer 里都不会出现它。页面本身是纯静态壳子，服务端不认识口令。
    """
    if not token:
        return ""
    return (f"{absolute_base(request)}/admin/copy"
            f"#t={urllib.parse.quote(token, safe='')}")


def issue_magic(request: Request) -> tuple[bool, str]:
    """生成一次性链接并发邮件。返回 (成功?, 说明)。"""
    from .mailer import magic_mail, send

    ok, why = magic_ready()
    if not ok:
        return False, why

    ip = client_ip(request)
    since = time.time() - settings.admin_magic_window
    used = store.count_magic(ip, since)
    if used >= settings.admin_magic_limit:
        _record(request, "deny", "magic-flood")
        return False, (
            f"{settings.admin_magic_window // 60} 分钟内已申请 {used} 次，稍后再试。"
        )

    token = secrets.token_urlsafe(32)
    token_id = hashlib.sha256(token.encode()).hexdigest()
    store.create_magic(token_id, ip=ip, ua=user_agent(request),
                       ttl=max(60, settings.admin_magic_ttl))
    link = f"{absolute_base(request)}/admin?k={token}"

    minutes = max(1, settings.admin_magic_ttl // 60)
    subject, text, html = magic_mail(
        link, minutes=minutes, ip=ip, ua=user_agent(request),
        # 用户要求邮件里带口令 + 口令边上要有复制按钮：邮件客户端不执行 JS，
        # 所以按钮指向 /admin/copy（口令在 # 片段里），在那里用剪贴板 API 真复制。
        token=(settings.admin_token or ""),
        copy_link=copy_link_for(request, settings.admin_token or ""),
    )
    sent, msg = send(magic_recipient(), subject, text, html)
    _record(request, "ok" if sent else "deny", "magic-sent" if sent else f"magic-{msg[:60]}")
    if not sent:
        return False, f"邮件没发出去：{msg}"
    return True, f"已发到 {mask_email(magic_recipient())}，{minutes} 分钟内有效。"


def redeem_magic(request: Request, token: str) -> Response | None:
    """用链接里的 k 换会话。token 不对/过期/用过 → None（落回普通登录页）。"""
    token = (token or "").strip()
    if len(token) < 20:
        return None
    token_id = hashlib.sha256(token.encode()).hexdigest()
    if not store.use_magic(token_id):
        _record(request, "deny", "magic-invalid")
        return None
    value, _ = create_session(request)
    _record(request, "ok", "login-magic")
    resp = RedirectResponse(clean_url(request), status_code=303)
    set_session_cookie(resp, request, value)
    return resp


async def handle_magic_request(request: Request) -> Response:
    """POST /admin/magic：登录页上那个「发到邮箱」按钮。"""
    if not admin_token_enabled():
        return message_page("后台未启用", "未配置 GATEWAY_ADMIN_TOKEN。", status_code=503)
    ok, msg = issue_magic(request)
    if ok:
        return message_page(
            "登录链接已发出",
            msg,
            status_code=200,
            back=True,
        )
    return message_page(
        "没发出去",
        msg,
        status_code=400,
        back=True,
    )


def set_recipient(addr: str) -> str:
    """保存收件邮箱（空字符串=清除）。返回规范化后的值。"""
    addr = (addr or "").strip()
    if addr and ("@" not in addr or " " in addr or len(addr) > 200):
        raise ValueError("邮箱格式不对")
    store.set_setting(EMAIL_KEY, addr)
    return addr


# ---------------------------------------------------------------- 令牌
def root_token_from(request: Request) -> str | None:
    """脚本/curl 用的口令来源：Authorization Bearer → URL ?token=。

    Cookie 不再承载口令（只承载会话），避免「口令长期躺在浏览器里」。
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    params = urllib.parse.parse_qs(urllib.parse.urlparse(str(request.url)).query)
    if params.get("token"):
        return params["token"][0]
    return None


def token_ok(candidate: str | None) -> bool:
    if not candidate or not settings.admin_token:
        return False
    return hmac.compare_digest(candidate, settings.admin_token)


def require_admin_token(request: Request) -> None:
    """后台 API 用：会话 或 令牌（脚本）都行；未配置令牌 503，未授权 401，被封 429。"""
    if not admin_token_enabled():
        raise HTTPException(status_code=503, detail="admin token not configured")
    if current_session(request):
        return
    tok = root_token_from(request)
    ip = client_ip(request)
    if tok and token_ok(tok):
        # 不记 ok：脚本每次调用都写审计会把表刷爆；只记「登录成功」和「失败」
        return
    if tok:
        until = block_until(ip)
        if until:
            _record(request, "deny", "blocked")
            raise HTTPException(status_code=429, detail="too many failures")
        _record(request, "deny", "bad-token")
        raise HTTPException(status_code=401, detail="invalid admin token")
    until = block_until(ip)
    if until:
        raise HTTPException(status_code=429, detail="too many failures")
    raise HTTPException(status_code=401, detail="invalid admin token")


# ---------------------------------------------------------------- 页面
def clean_url(request: Request, *, keep: tuple[str, ...] = ("tab", "theme", "q")) -> str:
    """去掉 token 的干净地址（保留 tab/theme 之类的展示参数）。"""
    params = urllib.parse.parse_qs(urllib.parse.urlparse(str(request.url)).query)
    pairs = []
    for k in keep:
        if params.get(k):
            pairs.append(f"{k}={urllib.parse.quote(params[k][0])}")
    qs = "&".join(pairs)
    return f"{prefix_of(request)}/admin" + (f"?{qs}" if qs else "")


def admin_page(request: Request) -> Response:
    """GET /admin 的唯一入口（替代 P9 的 check_admin_page）。

    分支：未配置令牌 → 503；会话有效 → 页面；带对令牌 → 签发会话并 302 去干净地址；
    被封禁 → 429；其余 → 401 登录页。
    """
    if not admin_token_enabled():
        return message_page(
            "后台未启用",
            "管理后台未配置 GATEWAY_ADMIN_TOKEN，已拒绝访问（避免公网裸奔）。",
            status_code=503,
        )

    if current_session(request) is not None:
        return _index_response()

    # 邮件里的链接（?k=...）优先：一次性 token 换会话后立刻从地址栏消失
    params = urllib.parse.parse_qs(urllib.parse.urlparse(str(request.url)).query)
    if params.get("k"):
        resp = redeem_magic(request, params["k"][0])
        if resp is not None:
            return resp
        return login_page(
            request,
            error="这个登录链接无效或已经用过了，重新发一封，或者直接输口令。",
            status_code=401,
        )

    token = root_token_from(request)
    ip = client_ip(request)

    if token and token_ok(token):
        # 一次性兑换：令牌 → 会话，然后从地址栏消失
        value, _ = create_session(request)
        _record(request, "ok", "login-token")
        resp = RedirectResponse(clean_url(request), status_code=303)
        set_session_cookie(resp, request, value)
        return resp

    if token:
        until = block_until(ip)
        if until:
            _record(request, "deny", "blocked")
            return message_page(
                "访问已被临时限制",
                f"该设备在 {settings.admin_fail_window // 3600} 小时内多次输入错误口令，"
                f"已封禁到 {time.strftime('%m-%d %H:%M', time.localtime(until))}。",
                status_code=429,
            )
        _record(request, "deny", "bad-token")
        return login_page(request, error="口令不对，请重新输入。", status_code=401)

    until = block_until(ip)
    if until:
        return message_page(
            "访问已被临时限制",
            f"多次口令错误，已临时封禁到 {time.strftime('%m-%d %H:%M', time.localtime(until))}。"
            "换网络（换 IP）或到服务器上执行 <code>scripts/unblock.sh</code> 解封。",
            status_code=429,
        )

    return login_page(request, error=None, status_code=401)


def _index_response() -> Response:
    from fastapi.responses import FileResponse

    from .config import PROJECT_ROOT
    resp = FileResponse(str(PROJECT_ROOT / "admin" / "index.html"))
    # 后台页面永远是「个人实时数据」，禁止任何中间层缓存
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _shell(title: str, body: str, *, status_code: int) -> HTMLResponse:
    html = (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title}</title><style>"
        ":root{color-scheme:light dark}"
        "*{box-sizing:border-box}"
        "body{margin:0;font-family:-apple-system,system-ui,'PingFang SC',sans-serif;"
        "background:#f2f2f7;color:#1c1c1e;display:flex;min-height:100vh;"
        "align-items:center;justify-content:center;padding:24px}"
        ".card{width:100%;max-width:400px;background:#fff;border-radius:16px;"
        "padding:24px;box-shadow:0 10px 30px rgba(0,0,0,.06)}"
        "h1{font-size:20px;line-height:26px;margin:0 0 6px;font-weight:600}"
        "p{font-size:14px;line-height:21px;color:#6e6e73;margin:0 0 14px}"
        "label{font-size:13px;line-height:18px;color:#6e6e73;display:block;margin-bottom:6px}"
        "input[type=password]{width:100%;height:44px;font-size:16px;padding:0 12px;"
        "border:1px solid #d1d1d6;border-radius:10px;font-family:inherit;background:#fff;"
        "color:#1c1c1e}"
        "input[type=password]:focus{outline:none;border-color:#007aff;"
        "box-shadow:0 0 0 3px rgba(0,122,255,.15)}"
        "button{width:100%;height:44px;margin-top:14px;border:none;border-radius:10px;"
        "background:#007aff;color:#fff;font-size:16px;font-weight:600;font-family:inherit}"
        "button:active{opacity:.85}"
        "button.ghost{background:transparent;color:#007aff;border:1px solid #007aff;margin-top:10px}"
        ".or{display:flex;align-items:center;gap:10px;margin:18px 0 2px;"
        "font-size:12px;color:#8e8e93}"
        ".or:before,.or:after{content:'';flex:1;height:1px;background:#e5e5ea}"
        "a.back{display:inline-block;margin-top:14px;font-size:14px;color:#007aff;"
        "text-decoration:none}"
        ".err{background:rgba(255,59,48,.1);color:#d70015;font-size:13px;line-height:18px;"
        "padding:8px 10px;border-radius:8px;margin-bottom:12px}"
        ".hint{font-size:12px;line-height:17px;color:#8e8e93;margin-top:14px}"
        "code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;"
        "background:#f2f2f7;padding:1px 5px;border-radius:4px}"
        "@media (prefers-color-scheme:dark){body{background:#000;color:#f5f5f7}"
        ".card{background:#1c1c1e;box-shadow:none}h1{color:#f5f5f7}p,label{color:#98989d}"
        "input[type=password]{background:#2c2c2e;border-color:#3a3a3c;color:#f5f5f7}"
        ".hint{color:#8e8e93}code{background:#2c2c2e;color:#f5f5f7}}"
        f"</style></head><body><div class='card'>{body}</div></body></html>"
    )
    resp = HTMLResponse(html, status_code=status_code)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def login_page(
    request: Request, *, error: str | None, status_code: int = 401
) -> HTMLResponse:
    action = f"{prefix_of(request)}/admin/login"
    next_url = clean_url(request)
    days = max(1, session_ttl() // 86400)
    ready, why = magic_ready()
    if ready:
        magic_block = (
            "<div class='or'>或者</div>"
            f"<form method='post' action='{prefix_of(request)}/admin/magic'>"
            "<button type='submit' class='ghost'>发一封登录链接到邮箱</button></form>"
            f"<div class='hint'>发到 <strong>{mask_email(magic_recipient())}</strong>"
            f" · {max(1, settings.admin_magic_ttl // 60)} 分钟内有效</div>"
        )
    else:
        magic_block = (
            "<div class='or'>或者</div>"
            f"<div class='hint'>邮件找回未配置：{why}</div>"
        )
    body = (
        "<h1>模型网关</h1>"
        f"<p>输一次口令，{days} 天内不活跃免登录。</p>"
        + (f"<div class='err'>{error}</div>" if error else "")
        + f"<form method='post' action='{action}'>"
        f"<input type='hidden' name='next' value='{next_url}'>"
        "<label for='t'>访问口令</label>"
        "<input id='t' name='token' type='password' autocomplete='current-password' "
        "autocapitalize='off' spellcheck='false' autofocus>"
        "<button type='submit'>登录</button></form>"
        + magic_block
    )
    return _shell("登录 · 模型网关", body, status_code=status_code)


def message_page(
    title: str, message: str, *, status_code: int, back: bool = False
) -> HTMLResponse:
    tail = "<a class='back' href='javascript:history.back()'>← 返回上一页</a>" if back else ""
    return _shell(title, f"<h1>{title}</h1><p>{message}</p>{tail}", status_code=status_code)


# 复制口令页：纯静态壳子，服务端不知道口令（口令在 # 片段里，随请求一起发不出去）。
# 邮件客户端不执行 JS，所以「口令边上的复制按钮」只能落到这个页面上完成真复制。
_COPY_PAGE = """<!doctype html><html lang='zh'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<meta name='referrer' content='no-referrer'>
<title>复制口令 · 模型网关</title><style>
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,system-ui,'PingFang SC',sans-serif;
background:#f2f2f7;color:#1c1c1e;display:flex;min-height:100vh;align-items:center;
justify-content:center;padding:24px}
.card{width:100%;max-width:420px;background:#fff;border-radius:16px;padding:24px;
box-shadow:0 10px 30px rgba(0,0,0,.06)}
h1{font-size:20px;line-height:26px;margin:0 0 6px;font-weight:600}
p{font-size:14px;line-height:21px;color:#6e6e73;margin:0 0 14px}
.tok{background:#f3f4f6;border:1px solid #e5e7eb;border-radius:10px;padding:14px 16px;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:17px;line-height:24px;
color:#111827;word-break:break-all;-webkit-user-select:all;user-select:all;margin-bottom:14px}
button{width:100%;height:48px;border:none;border-radius:10px;background:#007aff;color:#fff;
font-size:16px;font-weight:600;font-family:inherit}
button:active{opacity:.85}
button.done{background:#0F6E56}
.hint{font-size:12px;line-height:17px;color:#8e8e93;margin-top:14px}
.hint.warn{color:#b45309}
p:empty{display:none}
a.back{display:inline-block;margin-top:14px;font-size:14px;color:#007aff;
text-decoration:none}
@media (prefers-color-scheme:dark){body{background:#000;color:#f5f5f7}
.card{background:#1c1c1e;box-shadow:none}h1{color:#f5f5f7}p{color:#98989d}
.tok{background:#2c2c2e;border-color:#3a3a3c;color:#f5f5f7}}
</style></head><body><div class='card'>
<h1 id='ttl'>复制口令</h1>
<p id='lead'></p>
<div class='tok' id='tok'>（口令没有传过来）</div>
<button id='go' type='button'>复制口令</button>
<div class='hint' id='hint'>别转发给任何人</div>
<a class='back' href='javascript:history.back()'>← 返回</a>
</div>
<script>
(function () {
  var m = /[#&]t=([^&]*)/.exec(location.hash || '');
  var tok = m ? decodeURIComponent(m[1]) : '';
  var box = document.getElementById('tok');
  var go = document.getElementById('go');
  var hint = document.getElementById('hint');
  var lead = document.getElementById('lead');
  if (!tok) {
    go.disabled = true;
    go.textContent = '没有拿到口令';
    lead.textContent = '这个页面需要从邮件里的「复制」按钮打开。';
    hint.className = 'hint warn';
    hint.textContent = '口令被截断，回到邮件长按复制';
    return;
  }
  box.textContent = tok;
  function ok() {
    go.textContent = '已复制 ✓';
    go.className = 'done';
    hint.className = 'hint';
    hint.textContent = '已复制';
  }
  function fallback() {
    try {
      var r = document.createRange();
      r.selectNodeContents(box);
      var s = window.getSelection();
      s.removeAllRanges();
      s.addRange(r);
      if (document.execCommand('copy')) { ok(); return; }
    } catch (e) {}
    go.textContent = '已选中，请长按拷贝';
    hint.className = 'hint warn';
    hint.textContent = '浏览器禁止写剪贴板，口令已选中，长按拷贝';
  }
  go.addEventListener('click', function () {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(tok).then(ok, fallback);
    } else {
      fallback();
    }
  });
})();
</script></body></html>"""


def copy_page() -> HTMLResponse:
    """GET /admin/copy：把邮件里的口令一键拷进剪贴板。不需要登录。"""
    resp = HTMLResponse(_COPY_PAGE, status_code=200)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


# ---------------------------------------------------------------- 表单登录
async def handle_login(request: Request) -> Response:
    """POST /admin/login：校验口令 → 签发会话 → 跳回干净地址。"""
    if not admin_token_enabled():
        return message_page(
            "后台未启用", "未配置 GATEWAY_ADMIN_TOKEN，已拒绝访问。", status_code=503
        )
    ip = client_ip(request)
    until = block_until(ip)
    # 表单自己解析（application/x-www-form-urlencoded），不额外依赖 python-multipart
    try:
        raw = (await request.body()).decode("utf-8", "replace")
    except Exception:
        raw = ""
    form = urllib.parse.parse_qs(raw)
    token = (form.get("token") or [""])[0].strip()
    next_url = (form.get("next") or [""])[0]
    if not next_url.startswith("/"):
        next_url = f"{prefix_of(request)}/admin"

    if token_ok(token):
        store.record_auth_event(
            ip=ip, ua=user_agent(request), path="/admin/login", result="ok", reason="login"
        )
        value, _ = create_session(request)
        resp = RedirectResponse(next_url, status_code=303)
        set_session_cookie(resp, request, value)
        return resp

    if until:
        _record(request, "deny", "blocked")
        return message_page(
            "访问已被临时限制",
            f"多次口令错误，已临时封禁到 {time.strftime('%m-%d %H:%M', time.localtime(until))}。",
            status_code=429,
        )
    _record(request, "deny", "bad-token")
    return login_page(request, error="口令不对，请重新输入。", status_code=401)


async def handle_api_login(request: Request) -> JSONResponse:
    """POST /admin/api/login：页面内输口令换会话，返回 JSON 而不是跳转。

    为什么单独开一个接口：手机把后台「添加到桌面」后，打开的是已经缓存过的页面壳
    （standalone 窗口，没有地址栏）。会话一过期，页面上所有请求都 401，整页提示里
    如果只给一个跳转链接，用户在独立窗口里来回跳很容易退不出来。这里让提示页自己
    带输入框，提交后原地种 cookie 再 reload，不离开当前窗口。

    口令校验与表单登录共用同一套「失败计数 + 临时封禁」逻辑，不会成为新的爆破口子。
    """
    if not admin_token_enabled():
        return JSONResponse(
            {"ok": False, "error": "后台未启用（服务器未配置访问口令）"}, status_code=503
        )
    ip = client_ip(request)
    until = block_until(ip)
    if until:
        _record(request, "deny", "blocked")
        return JSONResponse(
            {
                "ok": False,
                "blocked_until": until,
                "error": "多次口令错误，已临时封禁到 "
                + time.strftime("%m-%d %H:%M", time.localtime(until)),
            },
            status_code=429,
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    token = str((body or {}).get("token") or "").strip()
    if not token:
        return JSONResponse({"ok": False, "error": "请输入口令"}, status_code=400)

    if not token_ok(token):
        _record(request, "deny", "bad-token")
        until = block_until(ip)
        return JSONResponse(
            {
                "ok": False,
                "blocked_until": until,
                "error": "口令不对"
                if not until
                else "口令不对，这台设备的失败次数已达上限，暂时不能再试。",
            },
            status_code=429 if until else 401,
        )

    value, _ = create_session(request)
    _record(request, "ok", "login-api")
    resp = JSONResponse({"ok": True, "ttl": session_ttl()})
    set_session_cookie(resp, request, value)
    return resp


async def handle_logout(request: Request) -> Response:
    revoke_current(request)
    resp = RedirectResponse(f"{prefix_of(request)}/admin", status_code=303)
    clear_session_cookie(resp, request)
    return resp


def security_snapshot(request: Request) -> dict[str, Any]:
    """「安全」标签的数据：当前会话、在线会话、找回邮箱、最近成功/失败、封禁名单。"""
    from .mailer import mail_status

    sess = current_session(request)
    since = _since_window()
    ready, why = magic_ready()
    return {
        "now": time.time(),
        "session": {
            "created_at": sess.get("created_at") if sess else None,
            "last_seen": sess.get("last_seen") if sess else None,
            "expires_at": sess.get("expires_at") if sess else None,
            "ttl": session_ttl(),
            "ip": sess.get("ip") if sess else client_ip(request),
            "current_ip": client_ip(request),
        },
        "policy": {
            "idle_ttl": session_ttl(),
            "max_ttl": settings.admin_session_max,
            "fail_limit": settings.admin_fail_limit,
            "fail_window": settings.admin_fail_window,
            "block_ttl": settings.admin_block_ttl,
            "magic_ttl": settings.admin_magic_ttl,
            "magic_limit": settings.admin_magic_limit,
            "magic_window": settings.admin_magic_window,
        },
        # P20：邮件找回
        "email": {
            "value": magic_recipient(),
            "masked": mask_email(magic_recipient()),
            "ready": ready,
            "reason": why,
            "from_env": settings.admin_email,
        },
        "mail": mail_status(),
        "magic": store.list_magic(5),
        "sessions": store.list_sessions(8),
        "logins": store.list_auth_events(result="ok", limit=8),
        "failures": store.list_auth_events(result="deny", limit=8),
        "blocked": [
            {**row, "until": (row["last_at"] or 0) + settings.admin_block_ttl}
            for row in store.failure_ips(since, 8)
            if row["n"] >= settings.admin_fail_limit
        ],
    }


def unblock(ip: str) -> None:
    """手动解封：写一条 unban 事件，之后只统计它之后的失败。"""
    store.record_auth_event(ip=ip, ua="admin", path="/admin/api/security/unban",
                            result="unban", reason="manual")
