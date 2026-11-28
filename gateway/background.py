"""后台维护任务（P33/P34）：定时同步账号、刷新探针、清理过期状态。

为什么需要后台线程：
- **账号同步**：签到系统每天刷新凭据，网关得跟上，否则过期的 token 会一直 401；
- **探针刷新**：实测免费判定有 TTL，过期了就退回目录价（而目录价是错的），
  所以要定期重测；
- **清理**：冷却表和探针表会越积越多，得定期修剪。

节流原则：
- 同步是幂等的（内容哈希没变就不动文件），跑勤一点没关系；
- 探针**要花钱**，所以受「每小时预算」限制，且只在到期时才重测；
- 全部在后台线程里做，不阻塞请求。
"""
from __future__ import annotations

import datetime
import os
import threading
import time
from typing import Any

from .config import settings

PROBE_INTERVAL = 1800.0   # 30 分钟刷新一轮探针（R2.36 起只探新模型冷启动）
# R2.36：健康探测用官方免费模型（hy4flash-free），零积分成本。
# 它是「上游链路通不通」的主动信号，和失败重试/冷却互补；因为免费可以勤一点。
HEALTH_INTERVAL = 1800.0  # 30 分钟用免费模型探一次链路健康
CLEAN_INTERVAL = 3600.0   # 1 小时清理一次
FREEPOOL_INTERVAL = 1800.0  # 30 分钟检测一次两平台官方免费模型（R1.5）

# P46：签到任务。对齐签到系统原来的时间（12:45），但改成「窗口内跑一次」而不是
# cron 定点 —— 后台线程是 30 秒轮询，刚好到点时可能在 sleep，所以给一个窗口。
SIGNIN_HOUR = 12
SIGNIN_MINUTE = 45
SIGNIN_WINDOW_MIN = 90     # 12:45–14:15 之间任意时刻跑一次
RENEW_INTERVAL = 21600.0   # 6 小时检查一次凭据续期
# R2.21：额度政策监控改为**每天一次**（用户 2026-11-23 拍板）。政策是天级慢变化，
# 且可用积分会被请求消耗、签到增量日报已有——每小时查是噪音。窗口内每 60s 检查，
# 由 quota_watch.run() 内部的当天 state 去重保证只跑一轮。
QUOTA_HOUR = 9
QUOTA_MINUTE = 0
QUOTA_WINDOW_MIN = 90      # 09:00–10:30 之间跑一次

_thread: threading.Thread | None = None
_lock = threading.Lock()
_last_error = ""          # P49：后台线程启动/运行错误，status() 里能看到


def _log_error(msg: str) -> None:
    """把后台错误写进调度日志（后台页/记录页都看得到），别只留在内存里。"""
    try:
        from .store import store
        store.log_schedule("background", msg[:480], "error")
    except Exception:
        pass


def enabled() -> bool:
    """后台任务开关。测试环境默认关（PYTEST_CURRENT_TEST 会被设上）。"""
    if os.environ.get("GATEWAY_BACKGROUND", "").strip().lower() in ("0", "off", "false"):
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return True


def _refresh_probes() -> dict[str, Any]:
    from . import probe as PROBE
    from .workbuddy_upstream import catalog, known_accounts

    # R2.36 修复：旧代码 `from .router import router` 导的是**模块**不是实例
    # （Router 实例在 app.py:38），一直 ImportError 被外层 try 吞掉——
    # 意味着后台探针（价格标定）从未真正跑过。改用 app 里的实例。
    from .app import router

    cfg = router.load_config()
    if not cfg.get("probe_enabled", True):
        return {"ok": False, "reason": "探针已关闭"}
    top_k = int(cfg.get("probe_top_k", 6))
    models = [m.get("model_name") for m in catalog()
              if (m.get("kind") or "chat") == "chat" and m.get("model_name")]
    if not list(known_accounts()) or not models:
        return {"ok": False, "reason": "没有账号或模型"}
    return PROBE.refresh_pool(models[:top_k])


def _run_health_probe() -> dict[str, Any]:
    """R2.36：用官方免费模型做一次链路健康探测，零积分成本。

    和价格标定探针分开：这个只验证「账号凭据 + 上游链路」是否通，
    结果不喂 costmodel（免费模型喂 0 没有标定价值）。失败会记一条 warn 日志，
    便于将来区分「免费池不可用」到底是额度问题还是链路问题。
    """
    from . import probe as PROBE
    from .app import router
    from .store import store
    from .workbuddy_upstream import known_accounts

    cfg = router.load_config()
    if not cfg.get("probe_enabled", True):
        return {"ok": False, "reason": "探针已关闭"}
    accounts = list(known_accounts())
    if not accounts:
        return {"ok": False, "reason": "没有可用账号"}
    mid = PROBE.health_probe_model()
    if not mid:
        return {"ok": False, "reason": "没有免费模型可探"}
    result = PROBE.health_check(accounts[0], mid)
    if not result.get("ok"):
        store.log_schedule(
            "health_probe",
            f"{accounts[0]}/{mid} 健康探测失败：{(result.get('error') or '')[:120]}",
            "warn",
        )
    return result


def _refresh_free_pool() -> dict[str, Any]:
    """定时检测两平台官方免费模型，变化记进调度日志（R1.5）。

    用户要的能力：官方免费模型会换代（3.1→3.2→4），要定时检测并刷新免费池。
    只认官方标记的免费模型（freepool 唯一口径）；Qoder 官方不标免费，如实为空。
    比对上次结果，上新/下架都记 info 日志，方便用户在后台看到免费池变化。
    """
    from . import freepool
    from .store import store

    try:
        result = freepool.refresh(force=True)
        if result["changed"]:
            parts = []
            if result["added"]:
                parts.append("新增免费：" + "、".join(result["added"]))
            if result["removed"]:
                parts.append("下架免费：" + "、".join(result["removed"]))
            store.log_schedule("freepool", "官方免费模型变化 · " + "；".join(parts), "info")
        return {"ok": True, **result}
    except Exception as exc:  # noqa: BLE001
        _log_error("免费池检测失败：%s: %s" % (type(exc).__name__, exc))
        return {"ok": False, "error": str(exc)[:200]}


def _cleanup() -> None:
    from .store import store

    try:
        store.purge_cooldowns()
        store.prune_probes(time.time() - 7 * 86400)
        store.prune_schedule_log(500)
    except Exception:
        pass


def _maybe_signin() -> None:
    """每天一次：签到 + 积分核对 + 邮件日报。快照里的 `_done` 保证重启不重发。"""
    from . import daily_report

    n = datetime.datetime.now()
    start_m = SIGNIN_HOUR * 60 + SIGNIN_MINUTE
    now_m = n.hour * 60 + n.minute
    if not (0 <= now_m - start_m <= SIGNIN_WINDOW_MIN):
        return
    today = n.date().isoformat()
    state = daily_report._load_state()
    if (state.get(today) or {}).get("_done"):
        return
    try:
        result = daily_report.run(mail=True)
        from .store import store
        # P49：邮件没发出去也要留痕。原来只按 fail_count 决定等级，
        # 「签到全成功但邮件没发」记成 info，日志里看不出问题。
        level = "warn" if (result["fail_count"] or not result.get("mail_sent")) else "info"
        store.log_schedule(
            "signin",
            "每日签到：%d 个账号，%d 成功 / %d 失败，邮件%s" % (
                result["total"], result["ok_count"], result["fail_count"],
                "已发送" if result["mail_sent"] else "未发送"),
            level,
        )
    except Exception as exc:  # noqa: BLE001
        # P49：原来这里 `except Exception: pass` —— 签到整体崩掉时日志里
        # 一条痕迹都没有，用户只会觉得「今天怎么没收到邮件」，无从排查。
        try:
            from .store import store
            store.log_schedule(
                "signin",
                "每日签到执行异常：%s: %s" % (type(exc).__name__, str(exc)[:180]),
                "error",
            )
        except Exception:
            pass


def _maybe_watch_quota() -> None:
    """R2.21：每天一次，只读采集额度政策 + 发一封汇总（政策变化写信里）。

    与签到同款「窗口内每 60s 检查 + 内部 state 去重」，避免服务在窗口外启动漏跑。
    """
    from . import quota_watch

    n = datetime.datetime.now()
    start_m = QUOTA_HOUR * 60 + QUOTA_MINUTE
    now_m = n.hour * 60 + n.minute
    if not (0 <= now_m - start_m <= QUOTA_WINDOW_MIN):
        return
    try:
        result = quota_watch.run()
        if result.get("skipped"):
            return
        parts = ["%d 个账号" % result["total"]]
        if result.get("changed"):
            parts.append("%d 项政策变化" % result["changed"])
        parts.append("汇总已发" if result.get("summary_sent") else "汇总未发")
        from .store import store
        store.log_schedule("quota", "额度政策：" + "，".join(parts),
                           "info" if result.get("summary_sent") else "warn")
    except Exception as exc:  # noqa: BLE001
        _log_error("额度监控失败：%s: %s" % (type(exc).__name__, exc))


RENEW_FAIL_ALERT_STREAK = 2     # 连续失败几次发邮件（每 6h 一次 → 2 次=12h）
RENEW_ALERT_COOLDOWN = 24 * 3600  # 同一账号提醒冷却：24h 内不重复轰炸


def _notify_renew_failures() -> None:
    """把连续续期失败且冷却期已过的账号汇总成一封卡片邮件。

    只发一封汇总（不是每账号一封），用 mailer.card_html 的签到日报式表格行，
    与每日日报逐像素同款。没配 SMTP/收件人时不发（不误报"发送失败"）。
    """
    from . import mailer
    from .config import settings
    from .store import store

    if not (settings.smtp_host and settings.smtp_user and settings.smtp_password
            and (settings.admin_email or os.environ.get("GATEWAY_NOTIFY_EMAIL"))):
        return  # 没配邮件通道——部署本来就不打算发，不当失败
    now = time.time()
    alerts = [
        s for s in store.renew_states()
        if s.get("fail_streak", 0) >= RENEW_FAIL_ALERT_STREAK
        and (not s.get("notified_at") or now - s["notified_at"] >= RENEW_ALERT_COOLDOWN)
    ]
    if not alerts:
        return
    rows = []
    # R2.48：账号名必须过 display_label 掩码——底层 label 可能是内部昵称
    # （如历史遗留的「主号」），直接塞进邮件会把内部称呼裸露给用户。
    # 用户已多次指出：登录的是多个平等账号，不存在「主号」，一律掩码显示。
    from .display import display_label
    for s in sorted(alerts, key=lambda x: -x.get("fail_streak", 0)):
        label, plat = display_label(s["account_label"]), s["platform"]
        streak = s["fail_streak"]
        last = datetime.datetime.fromtimestamp(s["last_fail_at"]).strftime("%m-%d %H:%M") \
            if s.get("last_fail_at") else "-"
        rows.append((plat, label, "需重新授权", "#A32D2D", "#FEE2E2",
                     "连续续期失败 %d 次 · 最近 %s｜请重新扫码登录" % (streak, last)))
    html = mailer.card_html(
        title="凭据续期失败提醒",
        subtitle="下列账号自动续期连续失败，可能到期掉线",
        badge="需处理", badge_fg="#A32D2D", badge_bg="#FEE2E2",
        rows=rows, cta="", link="", footnote="本邮件由模型网关自动发出 · 24 小时内同一账号只提醒一次",
        section_title="待处理账号", rows_style="table",
        grad="#7F1D1D,#A32D2D",
    )
    recipient = os.environ.get("GATEWAY_NOTIFY_EMAIL", "").strip() or settings.admin_email
    ok, err = mailer.send(recipient, "⚠ 模型网关：%d 个账号凭据续期失败" % len(alerts),
                          "凭据续期失败，请重新扫码登录", html)
    if ok:
        for s in alerts:
            store.mark_renew_notified(s["account_label"], s["platform"])
        store.log_schedule("renew_alert", "已发续期失败提醒：%d 个账号" % len(alerts), "info")
    else:
        store.log_schedule("renew_alert", "续期失败提醒发送失败：" + err[:120], "error")


def _renew_credentials() -> None:
    """每 6 小时扫一遍凭据：dt- 剩 ≤7 天就用 drt- 续，写回 0600 文件。

    WorkBuddy 的 refreshToken 是 `$wbEncrypted` 信封，服务器端续不了 —— 只在
    网关扫码写出的那份恰好是明文时才尝试，失败不阻塞、不记敏感信息。
    R2.33：连续失败达阈值发邮件提醒重新扫码；成功清零。
    """
    from . import daily_report, signin
    from .store import store

    for platform, label, path in daily_report._accounts():
        try:
            cred = signin._load_json(path)
            days = signin._days_left(
                (cred.get("auth") or {}).get("expiresAt")
                if platform == "WorkBuddy"
                else (cred.get("token_expires_at") or cred.get("expires_at")))
            if days is None or days > 7:
                continue
            result = (signin.wb_renew(label) if platform == "WorkBuddy"
                      else signin.qoder_renew(f"qoder:{label}"))
            ok = bool(result.get("ok"))
            store.renew_result(label, platform, ok=ok)
            store.log_schedule(
                "renew", "%s/%s 凭据续期%s" % (
                    platform, label, "成功" if ok else "失败（需人工处理）"),
                "info" if ok else "warn",
            )
        except Exception:
            pass
    _notify_renew_failures()


def _loop() -> None:
    last_probe = 0.0
    last_health = 0.0
    last_clean = 0.0
    last_renew = 0.0
    last_signin_check = 0.0
    last_freepool = 0.0
    while True:
        try:
            now = time.time()
            if now - last_probe >= PROBE_INTERVAL:
                last_probe = now
                _refresh_probes()
            if now - last_health >= HEALTH_INTERVAL:
                last_health = now
                _run_health_probe()
            if now - last_clean >= CLEAN_INTERVAL:
                last_clean = now
                _cleanup()
            if now - last_freepool >= FREEPOOL_INTERVAL:
                last_freepool = now
                _refresh_free_pool()
            if now - last_renew >= RENEW_INTERVAL:
                last_renew = now
                _renew_credentials()
            # ⚠️ 不能「一天只判断一次」：服务若在窗口之前启动，那一次判断
            # 落在窗口外就直接 return，之后当天再也不会重试 → 漏跑。
            # 签到和额度政策都用「窗口内每 60 秒检查 + 内部 state/_done 去重」。
            if now - last_signin_check >= 60.0:
                last_signin_check = now
                _maybe_signin()
                _maybe_watch_quota()
        except Exception:
            pass
        time.sleep(30.0)


def start() -> None:
    """启动后台线程（幂等）。

    P46：签到/续期是账号维护，和「推理上游选谁」无关 —— 即使 upstream=mock
    也要能跑签到（用户可能只用 Qoder）。原来写死 `upstream == workbuddy`
    会让纯 Qoder 部署永远不签到。

    P49：启动失败必须留下痕迹。原来 app.py 里 `except Exception: pass`
    把任何异常都吞了，线程没起来也没人知道 —— 表现就是「每天到点没邮件、
    凭据也不续期」，完全无从排查。
    """
    global _thread, _last_error
    if not enabled():
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        try:
            _thread = threading.Thread(target=_loop, name="gw-bg", daemon=True)
            _thread.start()
            _last_error = ""
        except Exception as exc:  # noqa: BLE001
            _last_error = "%s: %s" % (type(exc).__name__, exc)
            _log_error("后台线程启动失败：" + _last_error)


def status() -> dict[str, Any]:
    return {
        "running": bool(_thread is not None and _thread.is_alive()),
        "enabled": enabled(),
        "probe_interval": PROBE_INTERVAL,
        "freepool_interval": FREEPOOL_INTERVAL,
        "renew_interval": RENEW_INTERVAL,
        "quota_at": "%02d:%02d" % (QUOTA_HOUR, QUOTA_MINUTE),
        "signin_at": "%02d:%02d" % (SIGNIN_HOUR, SIGNIN_MINUTE),
        "last_error": _last_error,
    }
