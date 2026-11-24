"""每日签到日报（P46）：网关侧自动签到 + 积分核对 + 邮件卡片。

在网关侧自行生成，账号来源为网关自己的
凭据目录（`credentials/*.info` / `credentials/*.qoder.json`），不再读签到系统的
accounts 目录、不再拉起 `check_all.py` 子进程。

判定口径**照搬**（签到系统 README 第 4 节，不要改坏）：
  - WorkBuddy 指标 = checkin-activity-status 的 `total_credits`（累计签到积分，只增）
  - Qoder 指标     = usage 的 `addOnQuota.total`（累计发放的活动积分）
  - 可用余额       = WorkBuddy `TotalCredits` / Qoder `plan_remaining + addon_remaining`
  - 成功 = 指标日增 ≥ 100；>0 但 <100 = 成功+附注；接口说成功但没涨 = 需确认；
    两者都没有 = 失败；积分查询失败 = 需确认；活动下线 = info（不算失败）
  - 无基线（新号当天）：`api_ok` 为真即成功
  - 快照保护：抓取瞬时失败不许用空值覆盖当天已记好的基线
"""
from __future__ import annotations

import datetime
import html
import json
import os
import smtplib
import ssl
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from . import signin
from .config import PROJECT_ROOT, settings
from .display import display_label

EXPECTED_DAILY = signin.EXPECTED_DAILY
STATE_KEEP_DAYS = 60
MAIL_MAX_TRIES = 5   # P49：邮件发送连续失败这么多次后就认了，别无限重试
DISPLAY_NAME: dict[str, str] = {}  # 网关侧不再写死昵称→手机号映射；label 本身就是显示名


# ---------------------------------------------------------------- 快照
def _state_path() -> Path:
    d = Path(settings.data_dir).expanduser()
    d = d if d.is_absolute() else PROJECT_ROOT / d
    return d / "signin_report_state.json"


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


# ---------------------------------------------------------------- 账号与采集
def _accounts() -> list[tuple[str, str, Path]]:
    """(platform, label, credential_path)，来自网关自己的凭据目录。"""
    try:
        cred_dir = signin._cred_dir()
    except Exception:
        return []
    out: list[tuple[str, str, Path]] = []
    for p in sorted(cred_dir.glob("*.info")):
        out.append(("WorkBuddy", p.stem, p))
    for p in sorted(cred_dir.glob("*.qoder.json")):
        # 文件名就是 display_label（P45 的 write_credential 定的）
        out.append(("Qoder", p.name[: -len(".qoder.json")], p))
    return out


# R1.2：账号显示名（手机号掩码）的唯一口径已抽到 gateway/display.py。
# 日报、提醒邮件、后台页面全部复用它——此前三处各写一份，「主号」掩码漏了两处，
# 用户反复指出同一问题。PRIMARY_DISPLAY 保留为兼容别名，别处若仍引用不会崩。
PRIMARY_DISPLAY = os.environ.get("GATEWAY_SIGNIN_PRIMARY_DISPLAY", "")


def _mask_phone(name: str) -> str:
    """邮件里统一显示手机号掩码——委托给 display 模块（唯一口径）。"""
    return display_label(name)


def _collect_one(platform: str, label: str, path: Path) -> dict[str, Any]:
    e: dict[str, Any] = {
        "key": f"{'wb' if platform == 'WorkBuddy' else 'qoder'}:{label}",
        "app": platform,
        "name": _mask_phone(label),
        "metric": None, "available": None, "streak": None,
        "token_days": None, "api_ok": False, "notice": False, "note": "",
    }
    cred = signin._load_json(path)

    if platform == "Qoder":
        e["token_days"] = signin._days_left(
            cred.get("token_expires_at") or cred.get("expires_at"))
        token = cred.get("token") or ""
        if not token:
            e["status"] = "fail"
            e["note"] = "凭据里没有 token，需重新扫码授权"
            return e
        res = signin.qoder_claim(token)
        # P46 修正：Qoder 现在能自动续期（deviceToken/refresh），
        # 所以邮件里不再写「需重新导出登录态」——那是旧结论。
        if res["result"] in ("api_error", "confirming", "no_campaign"):
            res = signin.qoder_claim(token)  # 二次确认，不 sleep 60s 堵后台线程
        usage = signin.qoder_usage(token) or {}
        if usage:
            plan = usage.get("plan_remaining") or 0
            addon = usage.get("addon_remaining") or 0
            try:
                e["available"] = float(plan) + float(addon)
            except (TypeError, ValueError):
                pass
            raw = usage.get("addon_total")
            try:
                e["metric"] = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                pass
        ok = res["result"] in ("claimed", "already_claimed")
        e["api_ok"] = ok
        e["notice"] = res["result"] in ("no_campaign", "campaign_expired")
        e["gain"] = EXPECTED_DAILY if ok else None
        e["gain_detail"] = f"每日活动 {EXPECTED_DAILY}" if ok else ""
        e["note"] = "" if ok else {
            "no_campaign": "活动已结束：这个账号不在活动范围，不是签到故障",
            "campaign_expired": "活动已结束：本期活动已下线，不需要处理",
            "confirming": "领取结果待确认",
            "unauthenticated": "登录状态已失效，需在后台重新授权",
            "api_error": "接口调用失败",
        }.get(res["result"], "领取失败")
        return e

    # WorkBuddy：`.info` 里的 accessToken 已是明文 JWT，不需要 Electron
    auth = cred.get("auth") or {}
    exp = auth.get("expiresAt")
    e["token_days"] = signin._days_left(exp)
    try:
        res = signin.wb_claim(cred)
    except Exception as exc:  # noqa: BLE001
        e["note"] = f"签到失败：{type(exc).__name__}"
        return e
    ok = res.get("result") in ("claimed", "already_claimed")
    e["api_ok"] = ok
    e["notice"] = res.get("result") == "no_campaign"
    e["gain"] = res.get("amount") if ok else None
    e["streak"] = res.get("streak_days")
    e["note"] = "" if ok else {
        "unauthenticated": "凭据已失效，请在后台「添加账号」重新扫码",
        "no_campaign": "签到活动未开启",
    }.get(res.get("result"), res.get("detail") or "签到失败")
    st = signin.wb_status(cred)
    if st.get("ok"):
        if e["metric"] is None:
            e["metric"] = st.get("metric")
        if e["streak"] is None:
            e["streak"] = st.get("streak")
    if e["available"] is None:
        e["available"] = signin.wb_available(cred)
    return e


def collect() -> list[dict[str, Any]]:
    return [_collect_one(plat, label, path) for plat, label, path in _accounts()]


# ---------------------------------------------------------------- 判定与渲染
def classify(entry: dict[str, Any], prev: dict[str, Any] | None) -> tuple[str, str]:
    """旧口径照搬；info 必须排在 fail 之前，否则活动结束会天天误报失败。"""
    if entry.get("status") == "fail":
        return "fail", ""
    metric = entry.get("metric")
    if metric is None:
        return "warn", "积分查询失败，无法确认是否到账"
    prev_metric = (prev or {}).get("metric")
    if prev_metric is None:
        if entry.get("api_ok"):
            return "ok", ""
        if entry.get("notice"):
            return "info", ""
        return "fail", "接口显示今日未完成"
    delta = metric - prev_metric
    entry["delta"] = delta
    if delta >= EXPECTED_DAILY:
        return "ok", ""
    if delta > 0:
        return "ok", "积分有增加，但少于 %d" % EXPECTED_DAILY
    if entry.get("notice"):
        return "info", ""
    if entry.get("api_ok"):
        return "warn", "接口显示已完成，但累计积分没有增加"
    return "fail", "今日累计积分没有增加"


STATUS_STYLE = {
    "ok": ("成功", "#0F6E56", "#E1F5EE"),
    "warn": ("需确认", "#854F0B", "#FEF3C7"),
    "fail": ("失败", "#A32D2D", "#FEE2E2"),
    "info": ("！已结束", "#A32D2D", "#FEE2E2"),
}


def _fmt_num(value: Any) -> str:
    if value is None:
        return "-"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    return "%d" % num if num == int(num) else "%.1f" % num


def _gain_text(e: dict[str, Any]) -> str:
    if e.get("delta") is not None and e["delta"] <= 0:
        if not e.get("api_ok"):
            return "今日 0"
        claimed = ("接口记录 +%d" % e["gain"]) if e.get("gain") else "接口记录有收益"
        return "今日 0（%s，但累计积分未增加）" % claimed
    if e.get("gain") is None:
        if e.get("delta") and e["delta"] > 0:
            return "今日 +%d（累计增量）" % e["delta"]
        return "今日 0"
    text = "今日 +%d" % e["gain"]
    if e.get("gain_detail"):
        text += "（%s）" % e["gain_detail"]
    return text


def _token_text(e: dict[str, Any]) -> str:
    if e.get("token_days") is None:
        return ""
    left = round(e["token_days"])
    text = "令牌 %d 天后到期" % left
    if left > 14:
        return text
    if e["app"] == "Qoder":
        return text + "（到期前可在后台重新授权）"
    return text + "（到期前需重新登录：后台「添加账号」扫码，或桌面端重新导出 .info）"


def render_rows(entries: list[dict[str, Any]]) -> str:
    rows = ""
    for e in entries:
        label, color, bg = STATUS_STYLE[e["status"]]
        parts = []
        if e.get("available") is not None:
            parts.append("剩余可用 %s" % _fmt_num(e["available"]))
        parts.append(_gain_text(e))
        if e.get("streak") is not None:
            parts.append("连签 %s 天" % _fmt_num(e["streak"]))
        tok = _token_text(e)
        if tok:
            parts.append(tok)
        note = ""
        if e.get("note"):
            note_color = {"fail": "#A32D2D", "warn": "#B45309",
                          "info": "#475569"}.get(e["status"], "#B45309")
            note = ('<div style="margin-top:6px;font-size:12px;color:%s;word-break:break-all;">'
                    "%s</div>" % (note_color, html.escape(str(e["note"])[:200])))
        rows += """
        <div style="padding:14px 0;border-bottom:1px solid #f3f4f6;">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;">
                <div style="min-width:0;">
                    <span style="display:inline-block;padding:1px 8px;border-radius:4px;font-size:11px;font-weight:600;background:%(chip_bg)s;color:%(chip_fg)s;margin-right:6px;">%(app)s</span>
                    <span style="font-size:14px;font-weight:600;color:#111827;word-break:break-all;">%(name)s</span>
                </div>
                <span style="display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;background:%(bg)s;color:%(color)s;white-space:nowrap;">%(label)s</span>
            </div>
            <div style="font-size:12px;color:#6b7280;margin-top:6px;line-height:1.7;">%(line)s</div>
            %(note)s
        </div>""" % {
            "chip_bg": "#E6F1FB" if e["app"] == "WorkBuddy" else "#EEEDFE",
            "chip_fg": "#185FA5" if e["app"] == "WorkBuddy" else "#534AB7",
            "app": html.escape(e["app"]),
            "name": html.escape(str(e["name"])),
            "bg": bg, "color": color, "label": label,
            "line": html.escape(" ｜ ".join(parts)),
            "note": note,
        }
    return rows


def build_email_html(entries: list[dict[str, Any]], today: str,
                     no_baseline: bool, head_title: str, head_sub: str,
                     grad: str) -> str:
    vals = [e["available"] for e in entries if e.get("available") is not None]
    if vals:
        head_sub += " · 剩余%s" % _fmt_num(round(sum(vals)))
    footer = """
            <p style="font-size:12px;color:#9ca3af;line-height:1.7;margin:18px 0 0;">
                日增 ≥%(expected)d 记为成功；「！已结束」= 无可领活动。网关每日签到自动发送。
            </p>""" % {"expected": EXPECTED_DAILY}
    return """
    <div style="max-width:600px;margin:0 auto;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
        <div style="background:linear-gradient(135deg,%(grad)s);padding:24px;border-radius:12px 12px 0 0;">
            <h2 style="color:#fff;margin:0;font-size:20px;">%(title)s</h2>
            <p style="color:rgba(255,255,255,0.88);margin:8px 0 0;font-size:13px;">%(date)s · %(sub)s</p>
        </div>
        <div style="border:1px solid #e5e7eb;border-top:none;padding:24px;border-radius:0 0 12px 12px;">
            %(rows)s
            %(footer)s
        </div>
    </div>""" % {
        "grad": grad, "title": head_title, "date": today, "sub": head_sub,
        "rows": render_rows(entries), "footer": footer,
    }


def mail_configured() -> bool:
    """邮件功能是否已配置。

    区分两种「发不出去」：
      - **没配置**（没 SMTP / 没收件人）→ 部署本来就不打算发日报，
        不该当成「发送失败」无限重试；
      - **配置了但发送失败**（SMTP 报错）→ 这一轮没做完，当天的自动窗口
        应该还能重试。
    """
    recipient = os.environ.get("GATEWAY_NOTIFY_EMAIL", "").strip() or settings.admin_email
    return bool(settings.smtp_host and settings.smtp_user
                and settings.smtp_password and recipient)


def send_mail(subject: str, body_html: str) -> bool:
    """用网关已有的 SMTP 配置（P20 邮件找回那套），不新增凭据文件。"""
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_password):
        return False
    recipient = os.environ.get("GATEWAY_NOTIFY_EMAIL", "").strip() or settings.admin_email
    if not recipient:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from or settings.smtp_user
        msg["To"] = recipient
        msg.attach(MIMEText("签到日报：" + subject, "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))
        context = ssl.create_default_context()
        if settings.smtp_ssl:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                      context=context, timeout=30)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
            server.starttls(context=context)
        with server:
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.smtp_from or settings.smtp_user,
                            recipient, msg.as_string())
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 主流程
def run(mail: bool = True) -> dict[str, Any]:
    """跑一轮：签到 + 采集 → 判定 → 写快照 → 发信。

    返回值里只有统计数字与状态，不含凭据/token。
    """
    today = datetime.date.today().isoformat()
    state = _load_state()
    prev_date = None
    for key in sorted(state):
        if key < today:
            prev_date = key
    prev_map = state.get(prev_date, {}) if prev_date else {}

    entries = collect()
    for e in entries:
        status, note = classify(e, (prev_map or {}).get(e["key"]))
        e["status"] = status
        if note and not e.get("note"):
            e["note"] = note

    old_today = state.get(today) or {}
    new_today: dict[str, Any] = {}
    for e in entries:
        previous = old_today.get(e["key"]) or {}
        metric, available = e.get("metric"), e.get("available")
        if metric is None and previous.get("metric") is not None:
            metric = previous["metric"]
        if available is None and previous.get("available") is not None:
            available = previous["available"]
        new_today[e["key"]] = {
            "metric": metric,
            "available": available,
            "api_ok": bool(e.get("api_ok") or previous.get("api_ok")),
            "label": e.get("name"),
        }
    # ⚠️ `_done` 的语义是「今天这一轮彻底做完」，后台任务靠它保证一天只跑一次。
    # **邮件没发出去就不能算做完** —— 否则一次失败的手动操作会把当天 12:45 的
    # 自动窗口白白占掉，用户当天再也收不到日报，而且没有任何报错。
    # 所以先落盘状态（暂不含 _done），发信成功后再补上。
    state[today] = new_today
    for old in sorted(state)[:-STATE_KEEP_DAYS]:
        state.pop(old, None)
    _save_state(state)

    counts = {s: sum(1 for e in entries if e["status"] == s)
              for s in ("ok", "warn", "fail", "info")}
    ok, warn, fail, info = (counts[k] for k in ("ok", "warn", "fail", "info"))
    total = len(entries)

    if fail:
        head_title, grad = "签到有账号失败", "#6E2A2A,#8C3838"
        head_sub = "%d 个账号签到成功 · %d 个失败" % (ok, fail)
        if info:
            head_sub += " · %d 个活动已结束" % info
    elif warn:
        head_title, grad = "签到需要确认", "#6B4A17,#8A5F1F"
        head_sub = "%d 个账号签到成功 · %d 个待确认" % (ok, warn)
        if info:
            head_sub += " · %d 个活动已结束" % info
    else:
        head_title, grad = "签到完成", "#1F4D3F,#2A6455"
        if info and ok:
            head_sub = "%d 个账号已签到 · %d 个活动已结束" % (ok, info)
        elif info:
            head_sub = "%d 个账号的活动已下线，今天没有可领的积分" % info
        else:
            head_sub = "%d 个账号今日均已签到" % ok

    if fail:
        tail = "%d 个账号失败" % fail
    elif warn:
        tail = "%d 个待确认" % warn
    elif info:
        tail = "%d 个账号已签到" % ok
    else:
        tail = "%d/%d 成功" % (ok, total)
    subject = f"【签到日报】{today} · {tail}"
    body = build_email_html(entries, today, prev_date is None,
                            head_title, head_sub, grad)

    dump = os.environ.get("GATEWAY_SIGNIN_HTML_OUT")
    if dump:
        try:
            Path(dump).write_text(body, encoding="utf-8")
        except Exception:
            pass
    sent = send_mail(subject, body) if mail else False
    # P49：`_done` = 「今天这一轮彻底做完」。
    #   · 没要求发信（mail=False）        → 做完
    #   · 邮件压根没配置                   → 本来就不发，做完（否则无限重试）
    #   · 邮件发出成功                     → 做完
    #   · 邮件配置了但发送失败             → 不做完，让当天的自动窗口还能重试
    #     （最多 MAIL_MAX_TRIES 次，避免 SMTP 长期故障时无限循环）
    mail_failed = bool(mail) and not sent and mail_configured()
    tries = int(old_today.get("_mail_tries") or 0)
    if mail_failed:
        tries += 1
    new_today["_mail_tries"] = tries
    if not mail_failed or tries >= MAIL_MAX_TRIES:
        new_today["_done"] = time.time()
    _save_state(state)
    return {
        # ⚠️ 不能 `**counts` 直接展开：counts 里也有 `ok` 键，会把布尔成功标志
        # 覆盖成「成功账号数」（实测返回 ok=6，骗过了所有 if result["ok"] 判断）。
        "ok": True, "date": today, "total": total, "counts": counts,
        "ok_count": ok, "warn_count": warn, "fail_count": fail, "info_count": info,
        "head_title": head_title, "subject": subject, "mail_sent": sent,
        "mail_failed": mail_failed, "mail_tries": tries,
        "summary": [{"app": e["app"], "name": e["name"], "status": e["status"]}
                    for e in entries],
    }
