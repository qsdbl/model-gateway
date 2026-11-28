"""管理后台 API（P6）：四页数据只读接口。

- 账号页：平台/可用积分/查询时间/授权状态/是否参与路由
- 模型页：实时目录/当前倍率/优惠截止/工具·图片能力/上下文限制
- 路由页：对外模型名/手工排序/免费优先/收费开关/账号范围
- 请求记录页：实际平台·模型·账号/选择原因/耗时/错误/Token 与可取得实际扣费

安全：凭据路径只暴露文件名、不暴露完整路径或内容；Token估算/目录倍率/实际扣费分列。
"""
from __future__ import annotations

import datetime
import os
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from .admin_auth import require_admin_token
from .app import UPSTREAM, get_candidates, router  # noqa: F401
from .config import settings
from .store import store

admin = APIRouter(prefix="/admin/api")

# 平台标签：库里存 id，页面上给人看的名字。「前置标签」就是它（P16）。
PLATFORM_LABELS = {"workbuddy": "WorkBuddy", "qoder": "Qoder", "mock": "本地模拟"}

# 客户端的 Codex 配置里 model_instructions_file 要写成用户本机的绝对路径，
# 服务器不知道本机在哪，用环境变量覆盖，默认给当前这台机器。
CLIENT_PROJECT_DIR = Path(
    os.environ.get(
        "GATEWAY_CLIENT_PROJECT_DIR", ""
    )
)


def platform_label(pid: str | None) -> str:
    return PLATFORM_LABELS.get((pid or "").lower(), pid or "未知")


def _display_name(label: str) -> str:
    """展示名统一走 display.display_label（含可配置的昵称映射）。"""
    from . import daily_report
    return daily_report._mask_phone(label)


def public_base(request: Request) -> str:
    """对外根地址：优先环境变量，其次从反向代理头推断（含子路径前缀）。"""
    env = (settings.public_base or "").rstrip("/")
    if env:
        return env
    prefix = (request.headers.get("x-forwarded-prefix") or "").rstrip("/")
    host = request.headers.get("host") or str(request.base_url).split("//")[-1].strip("/")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{proto}://{host}{prefix}"

# 真实模式下账号/模型页的兜底数据源（P14）。
# 设计：DB 表有数据就用 DB（可审计历史快照）；DB 为空时按真实候选/目录实时查，
# 避免「部署后打开后台全是暂无数据」。实时查询带 60s 缓存，不随刷新打上游。
_live_models_cache: dict = {"at": 0.0, "rows": None}


def _live_accounts() -> list[dict]:
    """用真实候选构造账号行（余额复用 app 层 60s 缓存）。"""
    try:
        cands = get_candidates({})
    except Exception:
        return []
    out, seen = [], set()
    for c in cands:
        if c.account_label in seen:
            continue
        seen.add(c.account_label)
        out.append({
            "label": c.account_label,
            "platform": c.platform,
            "platform_label": platform_label(c.platform),
            "auth_status": c.auth_status,
            "balance": c.balance,
            "balance_unit": c.balance_unit,
            "balance_checked_at": c.price_fetched_at,
            "enabled": c.auth_status == "ok",
            "credential": "服务器本地凭据",
            "active": c.auth_status == "ok",
        })
    return out


def _active_preview():
    """当前调度会选的候选对象（含账号/实测价格），拿不到就返回 None。

    R1.3：**统一入口**。以前「挂实测价格」的代码只写在 api_models 的 DB 分支里，
    而线上 models 表是空的（目录不落库，实时从上游取），所以实际走的是
    `_live_models()` 那条提前 return 的分支 —— 挂价格的代码永远执行不到，
    active 卡片只能看目录倍率（hunyuan-chat 目录未标价）→ 显示「未标价」，
    用户以为它在偷偷扣费。
    """
    try:
        from . import scheduler as _SCHED
        return _SCHED.preview_candidate(est_tokens=200, mode="low")
    except Exception:
        return None


def _attach_preview_price(items: list[dict], preview=None) -> list[dict]:
    """把预览候选的实测价格（est_cost / free_upto）挂到对应模型项上。

    目录倍率是「上游标的价」，实测价格才是「这次这个长度的请求实际花多少」。
    两者不一致时以实测为准（用户关心的是后者）。
    """
    if preview is None:
        return items
    from .freepool import is_official_free
    for it in items:
        if it.get("model_name") == preview.model_name:
            it["est_cost"] = preview.est_cost
            it["free_upto"] = preview.free_upto or 0
            it["always_free"] = bool(getattr(preview, "measured_free", False))
            # R2.39：前端「免费」标签只认官方免费池身份，不用 est_cost==0 推断。
            # 付费模型短请求免扣（est_cost=0）≠ 免费池成员，这是两个概念。
            it["is_free_pool"] = bool(is_official_free(preview.model_name))
            break
    return items


def _live_models() -> list[dict]:
    """真实模型目录（带真实倍率/能力/上下文 + 是否当前使用中）。"""
    if settings.upstream != "workbuddy":
        return []
    import time
    from .workbuddy_upstream import catalog
    now = time.time()
    rows = catalog()
    if not rows:
        return _live_models_cache["rows"] or []
    # P48：和接入页 _active_upstream_model() 同口径（调度器预览），不再用
    # effective_model()（P15 手动锁/默认值）——否则模型 tab 显示 Hy4 flash、
    # 接入 tab 显示 space-bunny，同一个"当前模型"两页打架。
    active = _active_upstream_model()
    items = [{
        "platform": "workbuddy",
        "model_name": m["model_name"],
        "display_name": m.get("display_name") or m["model_name"],
        "public_name": settings.public_model,
        "is_free": m.get("is_free"),
        "price_factor": m.get("credits_factor"),
        "pricing": m.get("pricing", "unknown"),   # free|fixed|dynamic|unknown
        "promotion_until": None,
        "promotion_status": "—",
        "tools": m.get("tools"),
        "image": m.get("image"),
        "context_window": m.get("context_window"),
        "description": m.get("description"),
        "badge": m.get("badge"),
        "kind": m.get("kind", "chat"),
        "recommended": bool(m.get("recommended")),
        "is_default": bool(m.get("is_default")),
        "fetched_at": now,
        "active": m["model_name"] == active,
    } for m in rows]
    # R1.3：live 分支也要挂实测价格——线上 models 表为空，页面走的就是这条分支。
    _attach_preview_price(items, _active_preview())
    _live_models_cache["rows"] = items
    _live_models_cache["at"] = now
    return items


def _mask_path(p: str | None) -> str:
    """凭据路径只显示文件名，不暴露目录结构。"""
    if not p:
        return "—"
    return Path(p).name


def _platforms(items: list[dict]) -> list[dict]:
    """账号页的「平台前置标签」：按平台聚合。

    P44：这里原来只有 `active`（=「当前选中的账号」），页面上却写成
    「未参与调度」——WorkBuddy 碰巧对（当前账号一定在调度里），
    Qoder 就错了：它没被选成当前账号，但**授权过的账号照样参与调度**。
    所以拆成两个字段：
      routing —— 这个平台有几个账号**能参与路由**（Qoder 看授权状态）
      active  —— 里面有没有「当前选中的账号」（右上角那个 ✓）
    """
    out: dict[str, dict] = {}
    for it in items:
        pid = it.get("platform") or "unknown"
        row = out.setdefault(pid, {
            "platform": pid,
            "label": platform_label(pid),
            "count": 0,
            "routing": 0,
            "active": False,
        })
        row["count"] += 1
        row["active"] = row["active"] or bool(it.get("active"))
        if _in_routing(it):
            row["routing"] += 1
    return sorted(out.values(), key=lambda x: (not x["routing"], x["label"]))


def _in_routing(it: dict) -> bool:
    """这个账号当前是否参与调度——与 scheduler._candidate_pool 口径一致。

    2026-11-21 更正：账号页「5 个 vs 免费池 4 个」的根因不是账号页多算，而是
    scheduler 曾在**账号级**把余额≤0 的账号整体剔除，连官方免费模型(hy4flash-free)
    也误伤。服务器实调证明：余额 0 的账号照样能用免费模型（credit=0、余额不变）。
    正确修法是 scheduler 只剔除余额 0 的**付费模型**、保留免费模型（见 scheduler.py:116-126），
    免费池因此回到 5 个，与账号页天然统一。

    所以这里「参与调度」不再看余额：只要没停用、能推理（Qoder 看授权）就算参与——
    余额 0 的账号仍可用免费模型。
    """
    if it.get("inference") is False:
        return False
    if it.get("enabled") is False:
        return False
    return True


@admin.get("/accounts")
async def api_accounts(request: Request) -> dict:
    """账号页数据（P16 平台标签 + P18 积分/消耗 + P19 多账号）。

    余额与「已用/总量」都取平台账单口径（一次查询全带回来），
    「近 7 天」只能是网关自己的记录——官方客户端里的消耗我们看不到，
    所以两个口径分开写清楚，不混成一个数。
    """
    require_admin_token(request)
    items = _accounts_rich()
    return {"items": items, "platforms": _platforms(items)}


def _accounts_rich() -> list[dict]:
    """真实账号列表：凭据文件为准，DB 补充启用状态，账单补充余额与消耗。"""
    from . import modelstate
    from .app import _account_snapshot
    from .workbuddy_upstream import known_accounts

    usage = store.usage_by_account(30)
    db = {r["label"]: r for r in store.list_accounts()}
    # R2.38：凭据续期失败状态（renew_state）合并进账号行。
    # production 写 platform="WorkBuddy"/"Qoder"，测试写小写——按 lower 归一建索引。
    renew = {}
    try:
        for st in store.renew_states():
            key = (str(st.get("account_label") or "").lower(),
                   str(st.get("platform") or "").lower())
            renew[key] = {
                "fail_streak": int(st.get("fail_streak") or 0),
                "last_fail_at": st.get("last_fail_at"),
            }
    except Exception:
        pass
    accounts = known_accounts()
    active = modelstate.active_account()
    rows: list[dict] = []
    for label in accounts:
        billing, meta = _account_snapshot(label)
        row = db.get(label) or {}
        u = usage.get(label) or _empty_usage()
        # P27：账单只给「已用总量」，所以每次读到就存一条快照；
        # 今日消耗 = 最新快照 − 今天基线，这个数才和平台 App 里看到的一致。
        if billing and billing.get("used") is not None:
            try:
                store.record_usage_snapshot(label, billing.get("used"))
            except Exception:
                pass
            today = store.usage_today(label)
            u = {**u, "bill_today": today}
        else:
            u = {**u, "bill_today": {"delta": None, "note": "账单没读到"}}
        credits = {}
        if billing:
            credits = {
                "total": billing.get("total"),
                "capacity": billing.get("capacity"),
                "used": billing.get("used"),
                "ratio": billing.get("ratio"),
                "groups": billing.get("groups") or [],
                "plan": billing.get("plan"),
                "is_paid": billing.get("is_paid"),
            }
        rows.append({
            "label": label,
            "platform": "workbuddy",
            "platform_label": platform_label("workbuddy"),
            "auth_status": "ok" if billing else (row.get("auth_status") or "unknown"),
            "balance": (billing or {}).get("total"),
            "balance_unit": "credits_workbuddy",
            "balance_checked_at": (billing or {}).get("checked_at"),
            "enabled": bool(row.get("enabled", True)),
            "credential": {
                "file": (meta or {}).get("file") or f"{label}.info",
                "updated_at": (meta or {}).get("updated_at"),
                "expires_at": (meta or {}).get("expires_at"),
                "refresh_expires_at": (meta or {}).get("refresh_expires_at"),
                "nickname": (meta or {}).get("nickname") or "",
            },
            "credits": credits,
            "usage": u,
            "active": False,
            "selected": bool(active and active == label),
            # P33：同步来源与时间（账号是从哪个签到系统复制过来的）
            "source": row.get("source"),
            "last_sync_at": row.get("last_sync_at"),
            "quota": row.get("quota"),
            "note": row.get("note"),
            "inference": True,
        })
    # ---- Qoder 账号（P44）----
    #
    # Qoder 的「能不能推理」和 WorkBuddy 不一样：不是凭据文件在不在，
    # 而是**官方 CLI 的本地登录态**在不在（`<config-dir>/.auth/user`，
    # WASM 加密，我们只判断文件在不在，不读内容）。
    # 所以 Qoder 行多一个 `qoder` 字段，带授权状态和用户名，
    # 前端据此显示「授权 / 检查授权」按钮。
    try:
        from . import qoder_upstream as qu
        from .qoder_auth import status as auth_status_of
        q_files = qu.known_accounts()
    except Exception:
        q_files = {}
    for label, _path in q_files.items():
        row = db.get(label) or {}
        try:
            st = qu.auth_state(label)
        except Exception:
            st = {"authorized": False, "reason": "检查失败"}
        try:
            sess = auth_status_of(label)
        except Exception:
            sess = {"state": "idle"}
        balance = row.get("balance")
        authorized = bool(st.get("authorized"))
        rows.append({
            "label": label,
            "platform": "qoder",
            "platform_label": platform_label("qoder"),
            "auth_status": "ok" if authorized else "needs_auth",
            "balance": balance,
            "balance_unit": "credits_qoder",
            "balance_checked_at": row.get("balance_checked_at"),
            "enabled": bool(row.get("enabled", True)),
            "credential": {
                "file": Path(row.get("credential_path") or "").name or f"{label}.qoder.json",
                "updated_at": (qu.account_meta(label) or {}).get("updated_at"),
                "expires_at": (qu.account_meta(label) or {}).get("expires_at"),
                "refresh_expires_at": (qu.account_meta(label) or {}).get("refresh_expires_at"),
            },
            "credits": {},
            "usage": usage.get(label) or _empty_usage(),
            "active": False,
            "selected": False,
            "source": row.get("source"),
            "last_sync_at": row.get("last_sync_at"),
            "quota": row.get("quota"),
            "note": row.get("note"),
            # Qoder 只有授权成功的账号才参与推理路由
            "inference": authorized,
            "qoder": {
                "authorized": authorized,
                # username 展示口径与账号行标题一致（nick → 掩码手机号），
                # CLI 的原始 username 仍是底层标识，不改动。
                "username": _display_name(st.get("username") or ""),
                "uid": st.get("uid"),
                "reason": st.get("reason") or "",
                "checked_at": st.get("checked_at"),
                "auth_state": sess.get("state"),
                "auth_message": sess.get("message"),
            },
        })

    # DB 里有、凭据目录里没有的账号（老部署/测试插的/master 记录的）也要列出来，
    # 但明确标成「凭据不在服务器」，避免看起来像能用。
    # P33：Qoder 这类「只有签到凭据、没有推理凭据」的账号凭据文件是在的，
    # 所以不能一律标成缺失——要按 platform 和文件实际存在与否分开说。
    # P44：Qoder 账号已经在上面单独处理过了，这里跳过。
    known = set(accounts) | set(q_files)
    for label, row in db.items():
        if label in known:
            continue
        if (row.get("platform") or "") == "qoder":
            continue
        cred_path = row.get("credential_path") or ""
        has_file = bool(cred_path) and Path(cred_path).is_file()
        platform = row.get("platform") or "workbuddy"
        rows.append({
            "label": label,
            "platform": platform,
            "platform_label": platform_label(platform),
            "auth_status": (row.get("auth_status") or "unknown") if has_file else "missing",
            "balance": row.get("balance"),
            "balance_unit": row.get("balance_unit") or "credits_workbuddy",
            "balance_checked_at": row.get("balance_checked_at"),
            "enabled": bool(row.get("enabled")),
            "credential": {
                "file": _mask_path(cred_path),
                "missing": not has_file,
                "updated_at": row.get("updated_at"),
                "expires_at": row.get("expires_at"),
            },
            "credits": {},
            "usage": usage.get(label) or _empty_usage(),
            "active": False,
            "selected": False,
            "source": row.get("source"),
            "last_sync_at": row.get("last_sync_at"),
            "quota": row.get("quota"),
            "note": row.get("note"),
            "inference": platform == "workbuddy",
        })
    if not rows:
        # 既没有凭据文件、DB 也空（本地 mock/首次部署）：用实时候选兜底，
        # 避免手机打开后台是「暂无账号」。
        items = _live_accounts()
        for it in items:
            it.setdefault("active", True)
            it["platform_label"] = platform_label(it.get("platform"))
            it["credits"] = {}
            it["usage"] = usage.get(it["label"]) or _empty_usage()
            it["credential"] = {}
        return items

    # 当前账号：显式选择优先；没选就是余额最多的那个（和调度一致）
    if active and any(r["label"] == active for r in rows):
        for r in rows:
            r["active"] = r["label"] == active
    elif rows:
        best = max(rows, key=lambda r: r["balance"] if r["balance"] is not None else -1)
        best["active"] = True
    rows.sort(key=lambda r: r["balance"] if r["balance"] is not None else -1, reverse=True)
    # R1.9：每个账号挂一个 in_routing，前端据此标注「余额为 0 · 不参与调度」，
    # 与平台汇总的 routing 计数、真实候选池三处口径一致。
    # R2.38：凭据续期失败 / 已过期 → 账号行显示红色「重新授权」按钮。
    from . import signin as _signin
    for r in rows:
        r["in_routing"] = _in_routing(r)
        st = renew.get((str(r["label"]).lower(), str(r["platform"]).lower()), {})
        streak = int(st.get("fail_streak") or 0)
        # credential.expires_at 是秒时间戳；days_left 与后台展开区口径一致
        exp = (r.get("credential") or {}).get("expires_at")
        days = _signin._days_left(exp) if exp else None
        r["renew"] = {
            "fail_streak": streak,
            "last_fail_at": st.get("last_fail_at"),
            "days_left": days,
            # 阈值 2 与 background.RENEW_FAIL_ALERT_STREAK 一致；
            # days_left<=0 表示凭据文件里的时间戳已过期。
            "needs_reauth": streak >= 2 or (days is not None and days <= 0),
        }
    return rows


def _empty_usage() -> dict:
    return {"today": 0.0, "d7": 0.0, "d30": 0.0, "total": 0.0,
            "requests": 0, "last_at": None, "daily": []}


@admin.get("/usage/day")
async def api_usage_day(request: Request) -> dict:
    """某账号某天的 24 小时用量（P27）。

    用户要求：「点击后应该支持展开今天 24 小时的一个消耗情况，能体现什么时间段
    用的什么模型」。平台账单不给按小时明细，所以这里用网关自己的请求记录分桶——
    每条都带 model_name，能直接回答「几点、用哪个模型、多少次、多慢」。
    """
    require_admin_token(request)
    label = (request.query_params.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="label required")
    day = (request.query_params.get("date") or "").strip() or None
    if day and len(day) != 10:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    model = (request.query_params.get("model") or "").strip() or None
    # R1.1：__all__ = 全账号汇总；历史 personal-agent 归入 low 在 store 层处理。
    acct = None if label == "__all__" else label
    return {"ok": True, "label": label, "model": model or "all",
            **store.usage_day(acct, day, model)}


@admin.get("/usage/hour")
async def api_usage_hour(request: Request) -> dict:
    """某一小时的请求明细（点 24 小时柱子展开）。"""
    require_admin_token(request)
    label = (request.query_params.get("label") or "").strip()
    day = (request.query_params.get("date") or "").strip()
    hour = (request.query_params.get("hour") or "").strip()
    if not label or not day or hour == "":
        raise HTTPException(status_code=400, detail="label/date/hour required")
    if len(hour) == 1:
        hour = "0" + hour
    model = (request.query_params.get("model") or "").strip() or None
    acct = None if label == "__all__" else label
    rows = store.usage_hour_detail(acct, day, hour, model=model)
    return {
        "ok": True, "label": label, "date": day, "hour": hour,
        "items": [
            {
                "at": r["created_at"],
                "model": r["model_name"],
                "status": r["status"],
                "latency_ms": r["latency_ms"],
                "tokens": r["token_estimate"],
                "credit": r["actual_credit"],
                "stream": bool(r["stream"]),
                "public_model": r["public_model"],
                "platform": r["platform"],
                "account": r["account_label"],
            }
            for r in rows
        ],
    }


# ---------- 扫码添加账号（P27） ----------
# 平台没有开放的「扫码换 token」接口，唯一入口是 CLI 的登录流程（pty 里给一个
# copilot.tencent.com/login?state=… 链接，登录完把凭据写到 local_storage）。
# 这里把它包成 start/status/cancel 三步：前端把链接画成二维码，扫完把凭据
# POST 回 accounts/import，复用同一套校验与 0600 落盘。
#
# 注意：这三个接口必须用普通 def（不是 async def）。start() 要等 CLI 吐出链接
# （最长 20 秒），写成 async 会把事件循环堵死，整个网关的转发都会卡住。
@admin.post("/accounts/qr/start")
def api_qr_start(request: Request) -> dict:
    require_admin_token(request)
    from . import qrlogin
    return {"ok": True, **qrlogin.start()}


@admin.get("/accounts/qr/status")
def api_qr_status(request: Request) -> dict:
    require_admin_token(request)
    from . import qrlogin
    return {"ok": True, **qrlogin.status()}


@admin.post("/accounts/qr/cancel")
def api_qr_cancel(request: Request) -> dict:
    require_admin_token(request)
    from . import qrlogin
    return {"ok": True, **qrlogin.cancel()}


# ---------------------------------------------------------------- Qoder 授权
#
# P44：Qoder 的推理靠官方 CLI 的本地登录态（`<config-dir>/.auth/user`，
# WASM 加密），环境变量塞 token 会被完全忽略。所以「让这个账号能推理」
# 只能走设备码授权 —— 每个账号点一次，之后 CLI 自己续期。
#
# 这三个接口和上面的 WorkBuddy 扫码是同构的（start / status / cancel），
# 前端可以复用同一套弹窗。


@admin.post("/qoder/authorize")
def api_qoder_authorize(request: Request) -> dict:
    """发起 Qoder 设备码授权。

    - `?label=qoder:xxx`：**已有账号补 dt 授权**（账号行的「授权」按钮）；
    - `?mode=enroll`（不带 label）：**新增账号** —— 扫码一次即完整可用，
      不需要再去账号 tab 点第二次「授权」。返回里的 `key` 是会话标识，
      后续 status/cancel 用它。

    这三个接口都用 **query 参数**而不是 body：它们必须写成同步 `def`，
    FastAPI 会把它们丢到线程池执行——`qoderclicn status` 要等子进程退出
    （最长 40 秒），写成 async 会把整个网关的事件循环堵死。
    """
    require_admin_token(request)
    from . import qoder_auth
    label = request.query_params.get("label") or ""
    mode = (request.query_params.get("mode") or "").strip().lower()
    if mode == "enroll" or not label.strip():
        return qoder_auth.start("", mode="enroll")
    return qoder_auth.start(label, mode="reauth")


@admin.get("/qoder/authorize/status")
def api_qoder_authorize_status(request: Request) -> dict:
    """轮询授权进度（?key=...，兼容 ?label=...）。URL 过期会自动换新的。"""
    require_admin_token(request)
    from . import qoder_auth
    key = request.query_params.get("key") or request.query_params.get("label") or ""
    return qoder_auth.status(key)


@admin.post("/qoder/authorize/cancel")
def api_qoder_authorize_cancel(request: Request) -> dict:
    require_admin_token(request)
    from . import qoder_auth
    key = request.query_params.get("key") or request.query_params.get("label") or ""
    return qoder_auth.cancel(key)


@admin.post("/qoder/probe")
def api_qoder_probe(request: Request) -> dict:
    """检查授权状态 + 续期签到凭据 + 刷新余额（?label=...）。"""
    require_admin_token(request)
    label = (request.query_params.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="label 必填")
    from . import qoder_upstream as qu
    from .store import store

    renewed = None
    try:
        renewed = qu.renew_credential(label)
    except Exception as e:
        renewed = {"ok": False, "reason": f"{type(e).__name__}"}

    probe = qu.probe_account(label)
    balance = probe.get("balance")
    if balance is not None:
        try:
            store.update_balance(label, float(balance), "credits_qoder", time.time())
        except Exception:
            pass
    # 授权状态变了就更新库里的 auth_status（页面刷新能直接看到）
    try:
        store.upsert_account(
            label=label, platform="qoder",
            credential_path=str(qu._credential_path(label)),
            auth_status="ok" if probe.get("authorized") else "needs_auth",
            enabled=True,
        )
    except Exception:
        pass
    return {"ok": True, "probe": probe, "renewed": renewed}


@admin.post("/account")
async def api_select_account(request: Request) -> dict:
    """手动切换「当前账号」（body: {"account": "<label>"} 或 {"account": null} 恢复自动）。"""
    require_admin_token(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    account = (body or {}).get("account")
    from . import modelstate
    from .workbuddy_upstream import known_accounts
    if account is None:
        modelstate.set_active_account(None)
        return {"ok": True, **modelstate.state_snapshot()}
    if not isinstance(account, str) or not account.strip():
        raise HTTPException(status_code=400, detail="account must be a non-empty string")
    account = account.strip()
    if settings.upstream == "workbuddy" and account not in known_accounts():
        raise HTTPException(status_code=400, detail=f"unknown account: {account}")
    # R2.43：锁定前实测这个账号能不能用（账单接口可读 = 最小可用信号）。
    # 否则锁一个账单 401/失效的账号，调度池里根本没有它，锁定会被静默忽略、
    # 请求照旧走别的号——用户看着「已锁定」，实际没生效，还以为系统有 bug。
    if settings.upstream == "workbuddy":
        from .app import _account_snapshot
        # 先清缓存，避免拿 60s 前的旧结论误判刚重新授权的账号。
        from .app import _wb_balance_cache
        _wb_balance_cache.pop(account, None)
        billing, _meta = _account_snapshot(account)
        if not billing:
            raise HTTPException(
                status_code=400,
                detail=f"账号 {account} 当前读不到账单（凭据可能失效或未授权），"
                       "不能锁定。请先在账号行点「重新授权」恢复，再锁定。",
            )
    modelstate.set_active_account(account)
    return {"ok": True, **modelstate.state_snapshot()}


@admin.post("/accounts/import")
async def api_import_account(request: Request) -> dict:
    """导入一个账号的凭据（P19）：校验 → 0600 落盘 → 登记 → 立即可用，无需重启。"""
    require_admin_token(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    content = (body or {}).get("content") or ""
    label = (body or {}).get("label") or None
    filename = (body or {}).get("filename") or None
    if not isinstance(content, str) or len(content) > 200_000:
        raise HTTPException(status_code=400, detail="content 必填且不能超过 200KB")
    from .credentials import ImportError_, import_credential
    try:
        # import_credential 内部已用只读账单接口实测 token（_verify），
        # 通不过会抛 ImportError_ 被上面的 400 挡掉。能走到这里就是验证通过的。
        result = import_credential(content, label=label, filename=filename)
    except ImportError_ as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 让紧随其后的账号列表读到刚导入的新凭据，而不是 60s 旧缓存。
    try:
        from .app import _wb_balance_cache

        _wb_balance_cache.pop(result["label"], None)
    except Exception:
        pass
    # 登记进 DB（启用 + 余额），列表马上能看到；凭据内容不进 DB
    store.upsert_account(
        label=result["label"], platform="workbuddy",
        credential_path=str(Path(settings.credential_dir) / result["file"]),
        auth_status="ok", enabled=True,
    )
    if result.get("balance") is not None:
        store.update_balance(
            result["label"], result["balance"], "credits_workbuddy", time.time()
        )
    store.clear_renew_state(result["label"], "workbuddy")
    return {"ok": True, "account": result, "verified": True}


@admin.post("/accounts/remove")
async def api_remove_account(request: Request) -> dict:
    """把一个账号从网关彻底剔除：删凭据文件 + DB 登记 + 告警状态。"""
    require_admin_token(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    label = (body or {}).get("label") or ""
    platform = str((body or {}).get("platform") or "workbuddy").lower()
    if not isinstance(label, str) or not label.strip():
        raise HTTPException(status_code=400, detail="label 必填")
    label = label.strip()

    removed_files: list[str] = []
    cred_dir = Path(settings.credential_dir).expanduser()
    if not cred_dir.is_absolute():
        cred_dir = Path(__file__).resolve().parent.parent / cred_dir
    candidates = [cred_dir / f"{label}.info"]
    if platform == "qoder":
        bare = label.split(":", 1)[1] if ":" in label else label
        candidates.append(cred_dir / f"{bare}.qoder.json")
    for path in candidates:
        try:
            if path.is_file():
                path.unlink()
                removed_files.append(path.name)
        except Exception:
            pass

    store.delete_account(label)
    if not label.startswith("qoder:"):
        store.delete_account(f"qoder:{label}")
    try:
        store.clear_renew_state(label, "WorkBuddy")
        store.clear_renew_state(label, "Qoder")
    except Exception:
        pass
    try:
        from .app import _wb_balance_cache

        _wb_balance_cache.pop(label, None)
    except Exception:
        pass
    return {"ok": True, "label": label, "removed_files": removed_files}


@admin.get("/models")
async def api_models(request: Request) -> dict:
    require_admin_token(request)
    rows = store.list_models()
    if not rows:
        # R1.3：live 分支（线上实际走这条）已在 _live_models 里挂了实测价格，
        # 这里再兜一次底，保证「当前模型」卡片永远显示本次免费/扣费而非「未标价」。
        return {"items": _attach_preview_price(_live_models(), _active_preview()),
                "active_account": _active_upstream_account()}
    import time
    # P48：这里原来用 effective_model()（P15 手动锁/默认值），和接入页的
    # _active_upstream_model()（问调度器实际会选谁）口径不同 —— 同一个"当前模型"
    # 在模型 tab 显示 Hy4 flash、接入 tab 显示 space-bunny，用户以为出 bug。
    # 统一改成调度器口径；_active_upstream_model 定义在下方，运行时已解析，可前向调用。
    now = time.time()
    active = _active_upstream_model()
    # R1.2/R1.3：active 卡片显示「本次预览的实测价格」而不是目录倍率。
    # 目录 factor=None 的模型（如 hunyuan-chat）实测短请求免费，旧逻辑只看
    # price_factor → 显示「未标价」，用户以为它在偷偷扣费。这里把调度器预览
    # 候选的 est_cost/free_upto 带出去，前端优先用它显示「本次免费 / N 积分」。
    active_preview = _active_preview()
    items = []
    for r in rows:
        promo = r["promotion_until"]
        promo_status = "—"
        if promo is not None:
            promo_status = "生效中" if promo > now else "已过期"
        items.append({
            "platform": r["platform"],
            "model_name": r["model_name"],
            "display_name": r["model_name"],
            "public_name": r["public_name"],
            "is_free": (None if r["is_free"] is None else bool(r["is_free"])),
            "price_factor": r["price_factor"],        # 目录倍率（非扣费）
            "pricing": "unknown" if r["price_factor"] is None
                       else ("free" if r["price_factor"] == 0 else "fixed"),
            "promotion_until": promo,
            "promotion_status": promo_status,
            "tools": None if r["tools"] is None else bool(r["tools"]),
            "image": None if r["image"] is None else bool(r["image"]),
            "context_window": r["context_window"],
            "description": None,
            "badge": None,
            "kind": "chat",
            "recommended": True,
            "is_default": False,
            "fetched_at": r["fetched_at"],
            "active": r["model_name"] == active,
        })
    # 把预览候选的实测价格挂到 active 项上（只挂 active，避免影响列表排序）
    _attach_preview_price(items, active_preview)
    return {"items": items, "active_account": _active_upstream_account()}


@admin.get("/free-forecast")
async def api_free_forecast(request: Request) -> dict:
    """免费池健康度 + 耗尽预测（R1.1）。估算值，样本不足时如实说明。"""
    require_admin_token(request)
    from . import forecast
    return {"ok": True, **forecast.forecast()}


@admin.get("/state")
async def api_state(request: Request) -> dict:
    """当前生效模型（后台手动切换的结果）+ 默认值，供前端显示。"""
    require_admin_token(request)
    from . import modelstate
    from .workbuddy_upstream import DEFAULT_MODEL
    snap = modelstate.state_snapshot()
    # 顶栏要能一眼看出「当前是哪个平台、哪个账号」，不用点进账号页（P16/P19）
    account = None
    try:
        cands = get_candidates({})
        if cands:
            # P49：**账号**口径也统一到调度器。原来这里按「余额最多」挑一个
            # 账号展示，而调度是按「本次预计扣费 + 轮转摊平」挑的，两者经常
            # 不是同一个账号 —— 顶栏看着在用 A，实际请求打的是 B。
            act = _active_upstream()
            best = next((c for c in cands if c.account_label == act.get("account")), None)
            if best is None:  # 调度预览不可用时的兜底
                best = max(cands, key=lambda c: c.balance if c.balance is not None else -1)
            account = {
                "label": best.account_label,
                "platform": best.platform,
                "platform_label": platform_label(best.platform),
                "selected": bool(snap.get("active_account")),
                "count": len(cands),
            }
    except Exception:
        account = None
    return {
        # P49：顶栏 chipModel 必须和接入页 / 模型页 / 路由页同一个口径。
        # 统一走 _active_upstream_model()（问调度器实际会选谁），不再用
        # effective_model()（P15 手动锁 / 默认值）——之前顶栏显示 Hy4 flash，
        # 接入页显示 space-bunny，用户以为两个模型对不上。
        "active_model": _active_upstream_model(),
        "default_model": DEFAULT_MODEL,
        "manual": snap.get("active_model") is not None,
        "updated_at": snap.get("updated_at"),
        "public_model": settings.public_model,
        "account": account,
        # P49：后台线程状态一并透出。签到 / 凭据续期全靠这个线程，
        # 它要是没起来，页面却什么都不显示，用户只会以为「今天没邮件」。
        "background": _background_status(),
    }


def _background_status() -> dict[str, Any]:
    """后台维护线程的状态（跑没跑、下次签到时间、启动错误）。"""
    try:
        from . import background
        return background.status()
    except Exception as exc:  # noqa: BLE001
        return {"running": False, "error": "%s: %s" % (type(exc).__name__, exc)}


@admin.post("/model")
async def api_select_model(request: Request) -> dict:
    """手动切换上游模型（P15）。

    body: {"model": "<模型 id>"} 切换；{"model": null} 清除手动选择回默认。
    只允许切换到目录里真实存在的模型，避免写进不存在的 id 让上游 404。
    """
    require_admin_token(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    model = (body or {}).get("model")
    from . import modelstate
    if model is None:
        modelstate.clear_active_model()
        return {"ok": True, **modelstate.state_snapshot()}
    if not isinstance(model, str) or not model.strip():
        raise HTTPException(status_code=400, detail="model must be a non-empty string")
    model = model.strip()
    # 只有真实模式才做目录校验（mock 本地开发无真实目录，允许任意 id）
    if settings.upstream == "workbuddy":
        from .workbuddy_upstream import catalog
        ids = {r["model_name"] for r in catalog()}
        if ids and model not in ids:
            raise HTTPException(status_code=400, detail=f"unknown model: {model}")
    modelstate.set_active_model(model)
    return {"ok": True, **modelstate.state_snapshot()}


@admin.get("/routes")
async def api_routes(request: Request) -> dict:
    require_admin_token(request)
    cfg = router.load_config()
    return {
        "public_model": settings.public_model,
        # P49：和顶栏 / 接入页 / 模型页统一口径，不再用 effective_model()。
        "active_model": _active_upstream_model(),
        **cfg,
        # P49：后台任务状态（自动签到 / 凭据续期）。放在 **cfg 之后，
        # 免得被 cfg 里的同名键覆盖。
        "background": _background_status(),
        # P49：当前上游账号，与顶栏/模型页/接入页同一口径。
        "active_account": _active_upstream_account(),
    }


# ---------- P33/P34/P35/P37/P41：调度、探针、账号同步 ----------
def _model_view(est_tokens: int = 200, *, include_qoder: bool = False) -> list[dict]:
    """模型视图：目录 + 价格曲线 + 禁用状态 + 生效顺序。

    排序规则和调度器一致（auto：本次预计扣费升序；ordered：按后台拖拽的顺序），
    页面上看到的顺序就是实际调用的顺序，不用猜。

    P41：`cost` 不再是「这个模型免费吗」，而是「**跑这个长度的请求**要花多少」。
    免费只是一段长度区间（实测各模型 110~460 token 不等），所以必须带长度。

    R2.37：`include_qoder=True` 时合并 Qoder 目录（high 清单按平台勾选要用）。
    默认 False，避免探针/定价页多出没标定价格的 Qoder 模型。
    """
    from . import costmodel
    from .freepool import is_official_free
    from .store import store
    from .workbuddy_upstream import catalog

    cfg = router.load_config()
    disabled = set(cfg.get("disabled_models") or [])
    order = [str(x) for x in (cfg.get("model_order") or [])]
    order_idx = {m: i for i, m in enumerate(order)}
    strategy = (cfg.get("strategy") or "auto").lower()
    manual_model = cfg.get("manual_model") or None

    curves = costmodel.all_curves()
    # 「本次免费」的判定也按长度来：free_upto 是这个模型已知的免费长度上限。
    free_models = {mid for mid, c in curves.items() if c.free_upto >= est_tokens}

    # R2.37：合并两平台目录，供 high 清单按平台勾选。Qoder 模型此前不进
    # _model_view，导致 high 配了 Qoder 独有模型（如 DeepSeek-V4-Pro）却看不见。
    # 两个目录的模型名实测不重名（Qoder 14 个全独有），不会覆盖。
    all_models = [dict(m, platform="workbuddy") for m in catalog()]
    if include_qoder:
        from . import qoder_upstream as qu
        try:
            all_models += [dict(m, platform="qoder") for m in qu.catalog()]
        except Exception:
            pass

    items: list[dict] = []
    for m in all_models:
        if (m.get("kind") or "chat") != "chat":
            continue
        mid = m.get("model_name") or ""
        curve = curves.get(mid)
        factor = m.get("credits_factor")
        est_cost = curve.cost_at(est_tokens) if curve else None
        if est_cost is None:
            cost = factor if factor is not None else 9.99
        else:
            cost = est_cost
        items.append({
            "model": mid,
            "platform": m.get("platform") or "workbuddy",
            "display_name": m.get("display_name") or mid,
            "factor": factor,
            "measured_credit": curve.measured_min_credit if curve else None,
            "measured_at": None,
            "free": mid in free_models,
            # R2.39：官方免费池身份（只认 freepool 真源），与「本次免扣」分开。
            "is_free_pool": is_official_free(mid),
            "free_upto": curve.free_upto if curve else 0,
            "always_free": bool(curve and curve.always_free),
            "flat_credit": curve.flat_credit if curve else None,
            "points": len(curve.points) if curve else 0,
            "measured": bool(curve and curve.points),
            "disabled": mid in disabled,
            "manual": mid == manual_model,
            "tools": bool(m.get("tools", True)),
            "image": bool(m.get("image", False)),
            "est_cost": est_cost,
            "cost": round(float(cost), 4),
            "badge": m.get("badge"),
        })

    def key(it: dict) -> tuple:
        if it.get("manual"):
            rank = -1                       # 置顶的手动指定永远排第一
        elif strategy == "ordered" and it["model"] in order_idx:
            rank = order_idx[it["model"]]
        else:
            rank = 10_000
        return (rank, 0 if it["free"] else 1, it["cost"], it["model"])

    items.sort(key=key)
    for i, it in enumerate(items):
        it["rank"] = i + 1
    return items


@admin.get("/schedule")
async def api_schedule(request: Request) -> dict:
    """调度面板（P37）：策略、模型顺序、探针结果、冷却表、账号池、调度日志。"""
    require_admin_token(request)
    from . import costmodel
    from .store import store

    cfg = router.load_config()
    from . import modelstate
    # P41：预览用的请求长度。免费是一段长度区间，所以「谁最便宜」跟长度有关，
    # 页面上要给用户一个切换长度的入口。
    est_tokens = _preview_tokens(request)
    curves = costmodel.all_curves()
    # R1：两个模式的 profile 一起给前端，路由页要分区展示（low 策略 / high 强模型清单）。
    profiles = cfg.get("profiles") or {}
    low_prof = dict(profiles.get("low") or {})
    high_prof = dict(profiles.get("high") or {})
    return {
        "strategy": cfg.get("strategy", "auto"),
        "model_order": list(cfg.get("model_order") or []),
        # R2.6：low 列表显示顺序（纯 UI，不参与调度）。非空=用户拖过并锁定。
        "ui_low_order": list(cfg.get("ui_low_order") or []),
        # R1：low / high 两套配置。顶层键仍是 low 的镜像（兼容旧前端与 mock 路径）。
        "profiles": {
            "low": {
                "allow_paid": bool(low_prof.get("allow_paid", True)),
                "strategy": low_prof.get("strategy", "auto"),
                "model_order": list(low_prof.get("model_order") or []),
            },
            "high": {
                "allow_paid": bool(high_prof.get("allow_paid", True)),
                "strategy": high_prof.get("strategy", "ordered"),
                # high 的强模型清单 = model_order（Q3 拍板：勾选 DeepSeek 4.1Flash / 4Pro）
                "model_order": list(high_prof.get("model_order") or []),
            },
        },
        # R1：隐藏的上游笼统别名（auto/default/快速…），前端不再展示这些。
        "hidden_models": list(cfg.get("hidden_models") or []),
        # R1：high 积分预警阈值（百分比，不写死数字）
        "low_balance_alert": dict(cfg.get("low_balance_alert") or {}),
        "disabled_models": list(cfg.get("disabled_models") or []),
        # P40：手动指定模型（置顶，不是独占）
        "manual_model": cfg.get("manual_model") or None,
        # P15 遗留的「完全手动锁」还在不在？在的话要提示用户清掉——
        # 它会让整个自动调度失效（免费优先/轮转/付费兜底全都不生效）。
        "legacy_lock": modelstate.active_model(),
        "free_first": bool(cfg.get("free_first", True)),
        "allow_paid": bool(cfg.get("allow_paid", True)),
        "fact_preamble": bool(cfg.get("fact_preamble", True)),
        "est_tokens": est_tokens,
        # R2.35：轮转公平窗口（分钟）；0=退回历史累计口径。
        "rotation_window_minutes": int(cfg.get("rotation_window_minutes", 30)),
        "probe": {
            "enabled": bool(cfg.get("probe_enabled", True)),
            "mode": str(cfg.get("probe_mode", "coldstart")),  # R2.36
            "ttl": int(cfg.get("probe_ttl", 3600)),
            "budget_per_hour": int(cfg.get("probe_budget_per_hour", 12)),
            "top_k": int(cfg.get("probe_top_k", 6)),
        },
        "models": _model_view(est_tokens, include_qoder=True),
        # R2.37：high 平台过滤——只勾 Qoder 时 high 严格只用 Qoder（不可用就报错）。
        # 空数组 = 未配置 = 保持旧行为（不过滤）。
        "high_enabled_platforms": [
            str(x) for x in (high_prof.get("enabled_platforms") or [])
        ],
        # R2.5：近 7 天各模型实际调用次数（分 low/high），前端据此把
        # low/high 列表排成「实际调用情况」的顺序，而不是字典序。
        "model_calls": store.model_call_counts(7),
        "probes": store.list_probes()[:80],
        "pricing": {
            mid: {
                "points": len(c.points),
                "free_upto": c.free_upto,
                "flat_credit": c.flat_credit,
                "max_tokens": c.measured_max_tokens,
                "min_credit": min((p.credit for p in c.points), default=None),
                "max_credit": max((p.credit for p in c.points), default=None),
            }
            for mid, c in curves.items()
        },
        "cooldowns": store.list_cooldowns(),
        "accounts": [
            {k: v for k, v in r.items()
             if k in ("label", "platform", "enabled", "balance", "source", "expires_at",
                      "last_sync_at", "nickname", "note", "quota", "auth_status")}
            for r in store.list_accounts()
        ],
        "log": store.list_schedule_log(40),
        # P49：后台任务状态（自动签到 / 凭据续期），路由页展示。
        "background": _background_status(),
        # P49：当前上游模型 + 账号，和顶栏/模型页/接入页同一口径。
        "active_model": _active_upstream_model(),
        "active_account": _active_upstream_account(),
    }


def _preview_tokens(request: Request) -> int:
    """后台预览用的请求长度（?tokens=800）。默认 200（典型短请求）。"""
    try:
        raw = request.query_params.get("tokens")
        if raw:
            return max(0, min(200000, int(raw)))
    except (TypeError, ValueError):
        pass
    return 200


@admin.post("/schedule")
async def api_save_schedule(request: Request) -> dict:
    """保存调度策略（P37）：模型顺序、禁用列表、免费优先、付费兜底、探针参数。"""
    require_admin_token(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    body = body or {}
    cfg = router.load_config()

    if "strategy" in body:
        strategy = str(body["strategy"]).lower()
        if strategy not in ("auto", "ordered"):
            raise HTTPException(status_code=400, detail="strategy must be auto|ordered")
        cfg["strategy"] = strategy
    if "model_order" in body:
        order = body["model_order"]
        if not isinstance(order, list):
            raise HTTPException(status_code=400, detail="model_order must be a list")
        cfg["model_order"] = [str(x) for x in order]
    if "ui_low_order" in body:
        # R2.6：low 列表显示顺序，纯 UI，不写进 profiles、不参与调度。
        order = body["ui_low_order"]
        if not isinstance(order, list):
            raise HTTPException(status_code=400, detail="ui_low_order must be a list")
        cfg["ui_low_order"] = [str(x) for x in order]
    if "disabled_models" in body:
        disabled = body["disabled_models"]
        if not isinstance(disabled, list):
            raise HTTPException(status_code=400, detail="disabled_models must be a list")
        cfg["disabled_models"] = [str(x) for x in disabled]
    if "manual_model" in body:
        # P40：手动指定模型（置顶优先）。null / 空串 = 取消指定，回到纯自动。
        manual = body["manual_model"]
        if manual is None or (isinstance(manual, str) and not manual.strip()):
            cfg["manual_model"] = None
        else:
            mid = str(manual).strip()
            # 只允许指定目录里真实存在的模型，避免写个不存在的 id 让上游 404。
            if settings.upstream == "workbuddy":
                from .workbuddy_upstream import catalog
                ids = {r.get("model_name") for r in catalog()}
                if ids and mid not in ids:
                    raise HTTPException(status_code=400, detail=f"unknown model: {mid}")
            cfg["manual_model"] = mid
    if body.get("clear_legacy_lock"):
        # 清掉 P15 遗留的「完全手动锁」——它会让整个自动调度失效。
        from . import modelstate
        modelstate.clear_active_model()
    for key in ("free_first", "allow_paid", "fact_preamble", "probe_enabled"):
        if key in body:
            cfg[key] = bool(body[key])
    if "probe_ttl" in body:
        cfg["probe_ttl"] = max(300, int(body["probe_ttl"]))
    if "probe_budget_per_hour" in body:
        cfg["probe_budget_per_hour"] = max(0, min(200, int(body["probe_budget_per_hour"])))
    if "probe_top_k" in body:
        cfg["probe_top_k"] = max(1, min(36, int(body["probe_top_k"])))
    # R2.35：轮转窗口 0-1440 分钟；0=退回历史累计（回滚开关）。
    if "rotation_window_minutes" in body:
        cfg["rotation_window_minutes"] = max(0, min(1440, int(body["rotation_window_minutes"])))
    # R2.36：探针模式限枚举，防止写入非法值让 probe 逻辑走错分支。
    if "probe_mode" in body:
        mode = str(body["probe_mode"]).lower()
        cfg["probe_mode"] = mode if mode in ("coldstart", "all") else "coldstart"

    # R1：分模式配置。high 独立于顶层键（顶层 = low 镜像），
    # 后台写 high 时只改 profiles.high，不会被 save_config 的 low 兼容层覆盖。
    profiles = cfg.setdefault("profiles", {})
    low_prof = profiles.setdefault("low", {})
    high_prof = profiles.setdefault("high", {})
    for mode, target in (("low", low_prof), ("high", high_prof)):
        patch = body.get("profiles", {}).get(mode) if isinstance(body.get("profiles"), dict) else None
        if not isinstance(patch, dict):
            continue
        if "allow_paid" in patch:
            target["allow_paid"] = bool(patch["allow_paid"])
        if "strategy" in patch:
            st = str(patch["strategy"]).lower()
            if st not in ("auto", "ordered"):
                raise HTTPException(status_code=400, detail=f"{mode}.strategy must be auto|ordered")
            target["strategy"] = st
        if "model_order" in patch:
            if not isinstance(patch["model_order"], list):
                raise HTTPException(status_code=400, detail=f"{mode}.model_order must be a list")
            target["model_order"] = [str(x) for x in patch["model_order"]]
        if "enabled_platforms" in patch:
            # R2.37：high 平台过滤白名单。只允许 workbuddy/qoder，空数组=不过滤（保持旧行为）。
            ep = patch["enabled_platforms"]
            if not isinstance(ep, list):
                raise HTTPException(status_code=400,
                                    detail=f"{mode}.enabled_platforms must be a list")
            clean = [str(x).lower() for x in ep if str(x).lower() in ("workbuddy", "qoder")]
            target["enabled_platforms"] = list(dict.fromkeys(clean))  # 去重保序
        if mode == "low":
            # 顶层键是 low 的镜像（router.save_config 的兼容层会用顶层覆盖 low），
            # 所以 low 改这里时必须同时写顶层，否则刚改的 profiles.low 会被顶层的旧值盖回去。
            for key in ("allow_paid", "strategy", "model_order"):
                if key in patch:
                    cfg[key] = target[key]
    if "low_balance_alert" in body and isinstance(body["low_balance_alert"], dict):
        alert = dict(cfg.get("low_balance_alert") or {})
        alert.update(body["low_balance_alert"])
        # 比值钳到 (0,1]，窗口天数为正；防止前端误填 100 当 100%。
        alert["ratio"] = max(0.001, min(1.0, float(alert.get("ratio", 0.1))))
        alert["window_days"] = max(1, min(60, int(alert.get("window_days", 7))))
        alert["enabled"] = bool(alert.get("enabled", True))
        cfg["low_balance_alert"] = alert
    if "hidden_models" in body and isinstance(body["hidden_models"], list):
        cfg["hidden_models"] = [str(x) for x in body["hidden_models"]]

    router.save_config(cfg)
    return {"ok": True, **cfg, "models": _model_view()}


@admin.post("/probe")
async def api_run_probe(request: Request) -> dict:
    """手动跑一轮探针（P34）：量出哪些模型真的免费。

    探针自己也要花钱，所以受「每小时预算」限制；额度用完会直接告诉你。
    """
    require_admin_token(request)
    from . import probe as PROBE
    from .workbuddy_upstream import catalog, known_accounts

    cfg = router.load_config()
    top_k = int(cfg.get("probe_top_k", 6))
    models = [m.get("model_name") for m in catalog()
              if (m.get("kind") or "chat") == "chat" and m.get("model_name")]
    models = models[:top_k]
    if not list(known_accounts()) or not models:
        return {"ok": False, "error": "没有账号或没有候选模型"}
    result = PROBE.refresh_pool(models)
    return {**result, "models": _model_view()}


@admin.post("/probe/health")
async def api_run_health_probe(request: Request) -> dict:
    """R2.36：手动用官方免费模型做一次链路健康探测（不花积分）。"""
    require_admin_token(request)
    from . import probe as PROBE
    from .workbuddy_upstream import known_accounts

    accounts = list(known_accounts())
    if not accounts:
        return {"ok": False, "error": "没有可用账号"}
    mid = PROBE.health_probe_model()
    if not mid:
        return {"ok": False, "error": "没有免费模型可探"}
    result = PROBE.health_check(accounts[0], mid)
    return {**result, "account": accounts[0], "model": mid}


@admin.get("/probe")
async def api_probe_list(request: Request) -> dict:
    require_admin_token(request)
    from .store import store

    return {"items": store.list_probes()[:120]}


# P41：价格标定。免费是一段长度区间，单点探测只能测到一个长度，
# 所以这里一次打多档长度，把模型的价格曲线测出来。
# 成本：每模型约 0.01~0.15 积分（各档长度分别计费），受校准次数限制。
#
# **异步**（P41 线上实测后改的）：一次标定 = N 模型 × 5 档 = 5N 次真实请求，
# 实测每次约 7 秒（2 模型 10 次用了 70 秒）。同步接口会让浏览器干等好几分钟，
# 用户以为卡死。所以 POST 立刻返回、后台线程跑、GET 轮询进度。


@admin.post("/pricing/calibrate")
async def api_pricing_calibrate(request: Request) -> dict:
    """启动一次价格标定（后台跑，立刻返回）。"""
    require_admin_token(request)
    from . import pricing_task
    from .workbuddy_upstream import catalog

    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}

    catalog_ids = [m.get("model_name") for m in catalog()
                   if (m.get("kind") or "chat") == "chat" and m.get("model_name")]
    wanted = body.get("models")
    if isinstance(wanted, list) and wanted:
        models = [str(m) for m in wanted if str(m) in catalog_ids]
    else:
        limit = int(body.get("limit") or 8)
        models = catalog_ids[:max(1, min(36, limit))]

    if not models:
        return {"ok": False, "error": "没有候选模型"}

    result = pricing_task.start(models)
    return {"ok": True, **result}


@admin.get("/pricing/calibrate")
async def api_pricing_calibrate_status(request: Request) -> dict:
    """标定进度。"""
    require_admin_token(request)
    from . import pricing_task

    return {"ok": True, **pricing_task.snapshot()}


@admin.post("/pricing/reset")
async def api_pricing_reset(request: Request) -> dict:
    """清空价格观测点（重新标定前先清干净）。"""
    require_admin_token(request)
    from . import costmodel
    from .store import store

    try:
        body = await request.json()
    except Exception:
        body = {}
    model = (body or {}).get("model")
    n = store.clear_pricing_points(model)
    costmodel.reload()
    return {"ok": True, "cleared": n, "models": _model_view()}


@admin.get("/config")
async def api_config(request: Request) -> dict:
    """接入页需要的真实配置块（P16）。

    用户反馈：「应该提供对应配置项的一键复制（包括真实的 key），而不是复制之后
    还要拆开一个一个输入」。所以这里把每个客户端的完整配置拼好返回，前端只负责
    渲染 + 复制，不做二次拼接。

    安全：与后台同一套令牌鉴权；未配置 GATEWAY_API_KEY 时明说「本机未启用鉴权」，
    不编造一个假 key。
    """
    require_admin_token(request)
    base = public_base(request)
    api_base = f"{base}/v1"
    key = settings.api_key or ""
    key_line = key or "（本机未配置 GATEWAY_API_KEY）"
    model = settings.public_model
    # P49：接入页也带当前上游账号，与顶栏/模型页同源。
    active_acc = _active_upstream_account()
    instr = str(CLIENT_PROJECT_DIR / "docs" / "examples" / "codex-gateway-instructions.md")

    # P20/P22：手机 App 必须走 HTTPS（iOS ATS / Android cleartext 默认禁明文 http）。
    # 首选固定域名（sslip.io + Let's Encrypt，不依赖任何隧道进程，重启也不变）；
    # 只有固定域名不可用时才退回 Cloudflare quick tunnel（域名随进程重启而变）。
    from .tunnel import fixed_https_base, quick_tunnel_url

    fixed = fixed_https_base()
    tunnel = "" if fixed else quick_tunnel_url()
    https_base = fixed or tunnel
    https_api = f"{https_base}/v1" if https_base else ""
    mobile_api = https_api or api_base
    # P38：固定 HTTPS 与反代推断出来的接口地址常常是同一个（都带 /gw 前缀）。
    # 两栏一模一样时只留「接口地址」一栏，不然用户会以为是两个地址。
    same_as_api = https_api and https_api.rstrip("/") == api_base.rstrip("/")
    if fixed:
        mobile_note = "固定 HTTPS 域名（重启不变）"
    elif tunnel:
        mobile_note = "HTTPS 隧道（重启后域名会变）"
    else:
        mobile_note = "未检测到 HTTPS 入口，手机 App 可能连不上"

    blocks = [
        {
            "id": "openai",
            "title": "通用 OpenAI 兼容",
            "hint": "任何支持自定义 OpenAI 接口的客户端都能填这四项",
            "code": (
                "{\n"
                f'  "base_url": "{api_base}",\n'
                f'  "api_key": "{key_line}",\n'
                f'  "model": "{model}"\n'
                "}"
            ),
        },
        {
            "id": "workbuddy",
            "title": "WorkBuddy 自定义模型",
            "hint": "设置 → 模型 → 自定义模型 → 添加，按行填",
            # 逐项对照表：宁可横向滚动，也不要让「标签 + 地址」折成两行
            "nowrap": True,
            "code": (
                "供应商      Custom / 自定义（OpenAI 兼容）\n"
                "模型名      " + model + "\n"
                f"Base URL    {api_base}\n"
                "接口路径    /chat/completions\n"
                f"API Key     {key_line}\n"
                "工具调用    开启\n"
                "图片输入    开启（当前模型支持时）"
            ),
        },
        {
            "id": "workbuddy-json",
            "title": "WorkBuddy models.json",
            "hint": "粘到 ~/.workbuddy/models.json 的数组里，不用在界面逐项填",
            "code": (
                "[\n"
                "  {\n"
                f'    "id": "{model}",\n'
                '    "name": "Personal Agent Gateway",\n'
                '    "vendor": "Custom",\n'
                f'    "url": "{api_base}/chat/completions",\n'
                f'    "apiKey": "{key_line}",\n'
                '    "supportsToolCall": true,\n'
                '    "supportsImages": true,\n'
                '    "supportsReasoning": false,\n'
                '    "useCustomProtocol": true,\n'
                '    "maxOutputTokens": 0\n'
                "  }\n"
                "]"
            ),
        },
        {
            "id": "qoder",
            "title": "Qoder 自定义模型",
            "hint": "设置 → 模型 → 添加模型 → Provider 选 OpenAI Compatible",
            # P43：Qoder 桌面端（Node/undici）聊天必带 31 个工具 + developer 角色，
            # 之前每次都被上游 11128 拒掉；现已做角色适配，可用。
            "nowrap": True,
            "code": (
                "供应商      OpenAI Compatible\n"
                "接口类型    Chat Completions API（不是 Responses API）\n"
                "Base URL    " + api_base + "\n"
                "Model ID    " + model + "\n"
                f"API Key     {key_line}\n"
                "上下文窗口  128000\n"
                "视觉        开启\n"
                "思考模式    关闭"
            ),
        },
        {
            "id": "codex-cli",
            "title": "Codex CLI",
            "hint": "整份存成 ~/.codex/gateway.config.toml，然后 codex --profile gateway",
            "code": (
                "# ~/.codex/gateway.config.toml（整份覆盖，无需再设环境变量）\n"
                f'model = "{model}"\n'
                'model_provider = "gateway"\n'
                'sandbox_mode = "danger-full-access"\n'
                'approval_policy = "never"\n'
                f'model_instructions_file = "{instr}"\n'
                "include_permissions_instructions = false\n"
                "include_apps_instructions = false\n"
                "include_collaboration_mode_instructions = false\n"
                "\n[skills]\ninclude_instructions = false\n"
                "\n[skills.bundled]\nenabled = false\n"
                "\n[features]\nplugins = false\nmemories = false\nhooks = false\n"
                "\n[mcp_servers.node_repl]\nenabled = false\n"
                "\n[mcp_servers.code-review-graph]\nenabled = false\n"
                "\n[model_providers.gateway]\n"
                'name = "gateway"\n'
                f'base_url = "{api_base}"\n'
                'wire_api = "responses"\n'
                "requires_openai_auth = false\n"
                f'http_headers = {{ Authorization = "Bearer {key_line}" }}\n'
            ),
        },
        {
            "id": "codex-app",
            "title": "Codex App（桌面客户端）",
            "hint": "App 只读顶层 ~/.codex/config.toml；先备份原文件再整份替换",
            "code": (
                "# ~/.codex/config.toml（App 专用顶层配置）\n"
                f'model = "{model}"\n'
                'model_provider = "gateway"\n'
                'sandbox_mode = "danger-full-access"\n'
                'approval_policy = "never"\n'
                f'model_instructions_file = "{instr}"\n'
                "include_permissions_instructions = false\n"
                "include_apps_instructions = false\n"
                "include_collaboration_mode_instructions = false\n"
                "\n[features]\nplugins = false\nmemories = false\nhooks = false\n"
                "\n[skills]\ninclude_instructions = false\n"
                "\n[skills.bundled]\nenabled = false\n"
                "\n[model_providers.gateway]\n"
                'name = "gateway"\n'
                f'base_url = "{api_base}"\n'
                'wire_api = "responses"\n'
                "requires_openai_auth = false\n"
                f'http_headers = {{ Authorization = "Bearer {key_line}" }}\n'
            ),
        },
        {
            "id": "chatbox",
            "title": "Chatbox（手机端）",
            "hint": mobile_note,
            "code": (
                "API 模式      OpenAI API Compatible\n"
                f"API 域名      {mobile_api.rsplit('/v1', 1)[0]}\n"
                f"API Path      {mobile_api}\n"
                f"API Key       {key_line}\n"
                f"模型          {model}"
            ),
        },
        {
            "id": "curl",
            "title": "命令行验证",
            "hint": "返回一段 JSON 即连通；不需要另配环境变量",
            "code": (
                f"curl -s {api_base}/chat/completions \\\n"
                f'  -H "Authorization: Bearer {key_line}" \\\n'
                '  -H "Content-Type: application/json" \\\n'
                f'  -d \'{{"model":"{model}","messages":[{{"role":"user","content":"你好"}}]}}\''
            ),
        },
    ]
    return {
        "public_model": model,
        # R1：对外现在有两个模型名（low / high），接入页要一起给，
        # 否则用户只会把 low 填进客户端，永远用不到 high 的强模型策略。
        "low_model": settings.low_model,
        "high_model": settings.high_model,
        "api_key": key,
        "api_key_set": bool(key),
        "base_url": base,
        "api_base": api_base,
        "admin_url": f"{base}/admin",
        "upstream": settings.upstream,
        # P38：接入页「模型名」一行要同时说明「客户端填什么」和「背后实际是谁」，
        # 否则模型自述（P36）说的是上游模型，用户填的却是 personal-agent，两边对不上。
        "active_model": _active_upstream_model(),
        # P49：当前实际会选的账号（与顶栏/模型页同源）。
        "active_account": active_acc,
        # P20：手机端地址（HTTPS 隧道优先）+ 为什么必须 HTTPS 的一句话
        "access": {
            "direct_api": api_base,
            "https_api": "" if same_as_api else https_api,
            "mobile_api": mobile_api,
            "fixed_host": fixed,
            "tunnel_host": tunnel,
            "https_kind": "fixed" if fixed else ("tunnel" if tunnel else ""),
            "same_as_api": same_as_api,
            "note": mobile_note,
        },
        "blocks": blocks,
    }


def _active_upstream() -> dict[str, Any]:
    """全站唯一的「当前上游」口径来源（P49）：模型 + 账号一体。

    返回调度器此刻实际会选的那个候选：
      {"model": "hy4flash-free", "account": "<账号掩码>",
       "platform": "workbuddy", "platform_label": "WorkBuddy"}

    P49 之前只统一了**模型**口径（P40 接入页 → P48 模型页 → P49 顶栏/路由页），
    **账号**口径没管：顶栏按「余额最多」挑一个账号展示，而调度器是按
    「本次预计扣费升序 + 轮转摊平」挑的，两者经常不是同一个账号 —— 用户
    看顶栏以为在用 A 账号，实际请求打的是 B 账号。模型和账号本来就是一次
    选择的结果，必须同源。

    mock 模式没有真实候选，只给对外模型名（手动锁优先）。
    """
    out: dict[str, Any] = {
        "model": None, "account": None, "platform": None, "platform_label": None,
    }
    if settings.upstream != "workbuddy":
        # mock 模式的手动锁走 modelstate（app.build_plan 也是这么读的），
        # 顶栏要尊重它，否则测试 / 本地开发里点了手动切换却显示没变。
        model = settings.public_model
        try:
            from . import modelstate
            model = modelstate.active_model() or model
        except Exception:
            pass
        out["model"] = model
        return out
    try:
        from . import scheduler as SCHED
        cand = SCHED.preview_candidate()
        if cand is not None:
            out["model"] = cand.model_name
            out["account"] = cand.account_label
            out["platform"] = cand.platform
            try:
                out["platform_label"] = platform_label(cand.platform)
            except Exception:
                out["platform_label"] = cand.platform
            return out
    except Exception:
        pass
    try:
        from .workbuddy_upstream import effective_model
        out["model"] = effective_model()
    except Exception:
        out["model"] = settings.public_model
    return out


def _active_upstream_account() -> dict[str, Any] | None:
    """当前调度会选中的账号（给模型 tab 卡片显示用）。

    与 `_active_upstream()` 同源，模型和账号永远说的是同一次选择。
    """
    act = _active_upstream()
    if not act.get("account"):
        return None
    return {
        "label": act.get("account"),
        "platform": act.get("platform"),
        "platform_label": act.get("platform_label"),
    }


def _active_upstream_model() -> str:
    """全站唯一的「当前上游模型」口径来源（P49）。

    返回调度器此刻实际会选的上游模型 id（mock 模式返回对外模型名）。
    四个页面都调这一个函数，杜绝同一模型在不同 tab 显示不一致：
      - 顶栏 chip      → api_state
      - 模型 tab        → api_models / _live_models
      - 接入 tab        → api_config
      - 路由 tab        → api_routes

    历史：P40 修了接入页、P48 修了模型页，但顶栏 state 与路由页 routes
    仍用 `effective_model()`（P15 手动锁 / 环境变量默认值），于是顶栏显示
    Hy4 flash、接入页显示 space-bunny。P49 把剩下两处也收编到本函数。
    定义在下方、被上面的接口前向调用：Python 运行时解析名字，无问题。
    """
    model = _active_upstream().get("model")
    return model or settings.public_model


@admin.post("/test")
async def api_self_test(request: Request) -> dict:
    """真跑一次最小对话（P16）：返回 AI 的实际回复 + 实际上游模型 + 耗时 + 扣费。

    为什么不只 ping：用户要「返回模型名称（ai 回复的）才能判断」，只有真正走完
    上游一次请求才能证明 key、路由、模型、计费都是通的。
    """
    require_admin_token(request)
    from .workbuddy_upstream import credit_from_usage, effective_model

    started = time.monotonic()
    payload = {
        "model": settings.public_model,
        "messages": [{"role": "user", "content": "只回复四个字：连接正常"}],
        "max_tokens": 64,
        "temperature": 0,
    }
    try:
        result = UPSTREAM.build_nonstream_response(payload)
    except Exception as e:  # 上游错误原样回给后台，方便判断是限流还是配置错
        return {
            "ok": False,
            "error": str(e)[:300],
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    msg = ((result.get("choices") or [{}])[0].get("message") or {})
    usage = result.get("usage") or {}
    used_model = effective_model() if settings.upstream == "workbuddy" else "mock"
    return {
        "ok": True,
        "reply": (msg.get("content") or "").strip() or "（空回复）",
        "model": used_model,
        "public_model": settings.public_model,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "credit": credit_from_usage(usage),
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        },
    }


@admin.get("/switch-history")
async def api_switch_history(request: Request) -> dict:
    """最近的调度切换段（P48）：连续相同 (账号,模型) 折叠成一段。

    回答用户的问题「今天为什么从 176 切到 153」——每段带 reason（来自调度器
    落库的 choose_reason）和 switched_from（上一段是谁），不用再猜。
    """
    require_admin_token(request)
    try:
        limit = max(1, min(100000, int(request.query_params.get("limit", "300"))))
    except (TypeError, ValueError):
        limit = 300
    # R2.26：days 给定时按时间窗口取（最近 N 天全量），不再受 limit 条数限制。
    # 前端「最近使用」聚合需要看全一周，否则近期 low 密集会把 high 挤出窗口。
    days = None
    if "days" in request.query_params:
        try:
            days = max(1, min(90, int(request.query_params.get("days"))))
        except (TypeError, ValueError):
            days = None
    return {"ok": True, "items": store.switch_history(limit, days=days)}


@admin.get("/requests")
async def api_requests(request: Request) -> dict:
    require_admin_token(request)
    rows = store.list_requests(limit=100)
    return {"items": [{
        "id": r["id"],
        "created_at": r["created_at"],
        "public_model": r["public_model"],
        "platform": r["platform"],
        "model_name": r["model_name"],
        "account_label": r["account_label"],
        "choose_reason": r["choose_reason"],
        "status": r["status"],
        "error": r["error"],
        "latency_ms": r["latency_ms"],
        "token_estimate": r["token_estimate"],      # 估算（非账单）
        "price_factor": r["price_factor"],          # 目录倍率（非账单）
        "actual_credit": r["actual_credit"],        # 可取得的真实扣费；NULL=未知
        "stream": bool(r["stream"]),
    } for r in rows]}


@admin.get("/security")
async def api_security(request: Request) -> dict:
    """「安全」页：当前会话 + 策略 + 最近登录/失败 + 封禁名单（P17）。"""
    from .admin_auth import security_snapshot
    require_admin_token(request)
    return security_snapshot(request)


@admin.post("/security/unban")
async def api_unban(request: Request) -> dict:
    """解除某个 IP 的封禁（口令错误太多次时自己把自己锁了，得有出口）。"""
    require_admin_token(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    ip = str((body or {}).get("ip") or "").strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip required")
    from .admin_auth import unblock
    unblock(ip)
    return {"ok": True, "ip": ip}


@admin.post("/security/email")
async def api_set_email(request: Request) -> dict:
    """保存「找回邮箱」（P20）。只存地址，SMTP 口令始终在 .env。"""
    from .admin_auth import mask_email, set_recipient

    require_admin_token(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    try:
        addr = set_recipient(str((body or {}).get("email") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "email": addr, "masked": mask_email(addr)}


@admin.get("/mail-settings")
async def api_get_mail_settings(request: Request) -> dict:
    """读取 SMTP 配置（安全页用）。授权码只回「是否已设置」，绝不回明文。"""
    require_admin_token(request)
    from .mailer import MAIL_KEYS, _smtp_cfg
    from .store import store

    c = _smtp_cfg()
    out = {k: c[k] for k in ("host", "port", "user", "from", "ssl")}
    out["password_set"] = bool(c["password"])
    # 每个字段的来源：存库 or 环境变量（前端提示「留空沿用环境变量」）
    out["stored"] = {k: bool(store.get_setting("mail_" + k, "")) for k in MAIL_KEYS}
    return out


@admin.post("/mail-settings")
async def api_set_mail_settings(request: Request) -> dict:
    """保存 SMTP 配置到 SQLite settings 表。授权码留空 = 保留原值。"""
    require_admin_token(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    body = body or {}
    from .mailer import MAIL_KEYS
    from .store import store

    # 只接受白名单键；授权码为空字符串时跳过写入（保留原值）。
    for k in MAIL_KEYS:
        if k not in body:
            continue
        val = body[k]
        if k == "smtp_password" and (val is None or str(val) == ""):
            continue
        if k == "smtp_ssl":
            val = "1" if str(val).lower() in ("1", "true", "yes", "on") else "0"
        else:
            val = str(val if val is not None else "").strip()
        store.set_setting("mail_" + k, val)
    from .mailer import mail_status
    return {"ok": True, **mail_status()}


@admin.post("/security/email/test")
async def api_test_email(request: Request) -> dict:
    """发一封测试邮件，用来确认 SMTP 与收件箱都是通的。"""
    from .admin_auth import absolute_base, copy_link_for, magic_recipient, mask_email
    from .mailer import magic_mail, send

    require_admin_token(request)
    to = magic_recipient()
    if not to:
        return {"ok": False, "error": "还没设置收件邮箱"}
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "")
    subject, text, html = magic_mail(
        f"{absolute_base(request)}/admin", minutes=0, ip=ip or "本机",
        ua="SMTP 测试", test=True, token=(settings.admin_token or ""),
        copy_link=copy_link_for(request, settings.admin_token or ""),
    )
    ok, msg = send(to, subject, text, html)
    return {"ok": ok, "to": mask_email(to), "message": msg}


# ---------------------------------------------------------------- P46 签到
@admin.get("/signin/status")
def api_signin_status(request: Request) -> dict:
    """后台维护任务状态 + 最近一次日报快照（只读，不含凭据）。"""
    require_admin_token(request)
    from . import background, daily_report

    state = daily_report._load_state()
    today = datetime.date.today().isoformat()
    entry = dict(state.get(today) or {})
    entry.pop("_done", None)
    return {
        "ok": True,
        "background": background.status(),
        "today": today,
        "snapshot": entry,
        "accounts": [
            {"app": p, "label": label} for p, label, _ in daily_report._accounts()
        ],
    }


@admin.post("/signin/run")
def api_signin_run(request: Request) -> dict:
    """手动触发一轮签到+日报（同步执行，约 10–60 秒；带 ?mail=1 才发信）。

    排在线程池里跑（同步 def），不堵事件循环。
    """
    require_admin_token(request)
    from . import daily_report

    mail = request.query_params.get("mail", "") in ("1", "true", "yes")
    try:
        return daily_report.run(mail=mail)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------- R2.20 额度监控
@admin.get("/quota/status")
def api_quota_status(request: Request) -> dict:
    """额度监控状态 + 各账号最新快照 + 最近历史（只读，不含凭据）。

    `?label=xxx&limit=168` 可看单账号历史曲线数据。
    """
    require_admin_token(request)
    from . import daily_report
    from .store import store

    label = request.query_params.get("label", "").strip()
    if label:
        try:
            limit = min(int(request.query_params.get("limit", "168")), 1000)
        except ValueError:
            limit = 168
        return {
            "ok": True, "label": label,
            "latest": store.latest_quota(label),
            "history": store.quota_history(label, limit),
        }
    # 所有账号的最新一条
    out = []
    for _p, lbl, _path in daily_report._accounts():
        q = store.latest_quota(lbl)
        if q:
            out.append(q)
    return {"ok": True, "accounts": out, "count": len(out)}


@admin.post("/quota/run")
def api_quota_run(request: Request) -> dict:
    """手动触发一轮额度政策采集（同步只读，约数秒；带 ?mail=1 才发信）。

    `?force=1` 无视「当天已发」去重，立即重采重发（后台「发测试汇总」用）。
    兼容旧参数名 `force_summary`。
    """
    require_admin_token(request)
    from . import daily_report, quota_watch

    mail = request.query_params.get("mail", "") in ("1", "true", "yes")
    force = (request.query_params.get("force", "")
             or request.query_params.get("force_summary", "")) in ("1", "true", "yes")
    try:
        return quota_watch.run(send=daily_report.send_mail if mail else False,
                               force=force)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
