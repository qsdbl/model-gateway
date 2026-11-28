"""分模式邮件提醒（R1 阶段4）。

两种提醒，触发条件完全不同：
- low：「本次请求所有免费模型都用不了 → 开始选付费模型」那一刻，每天只发一封
  （含当日累计扣费）；标记按天滚动，第二天可再发。
- high：某账号余额跌破「近 7 日峰值 × ratio」阈值时发一封，回升后重置，可再次触发。
  WB / Qoder 积分是不同货币，阈值按各自账号的历史峰值算，**绝不共用数字**。

设计要点：
- 发信一律走 mailer.send()（不抛异常）；收件人 GATEWAY_NOTIFY_EMAIL 或 settings.admin_email。
- 去重/状态存 store 的 settings 表（现成的 kv，跨进程安全），**不碰签到日报的 _done**。
- 所有函数失败都吞掉（提醒不能把正常请求搞挂），只写 schedule_log 留痕。
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime
from typing import Any

from . import mailer
from .config import settings
from .display import display_label
from .store import store

_PREFIX = "notify.r1."


def _money(value: float | None, unit: str = "积分") -> str:
    """金额统一两位小数——此前 2530.94000002 这种浮点尾数直接进邮件，太丑。"""
    if value is None:
        return "未知"
    return f"{float(value):.2f} {unit}".strip()


# 余额历史：每个账号滚动存近 N 天的 (ts, balance)，用来算峰值
_BAL_HISTORY_KEY = _PREFIX + "balance_history"
_MAX_HISTORY_DAYS = 8          # 多留一天边界
_MAX_POINTS_PER_ACCOUNT = 200


def _recipient() -> str:
    return (os.environ.get("GATEWAY_NOTIFY_EMAIL", "").strip()
            or settings.admin_email or "").strip()


def _enabled() -> bool:
    return bool(_recipient() and mailer.mail_configured())


def _log(event: str, detail: str) -> None:
    try:
        store.log_schedule(event, detail[:480], "info")
    except Exception:
        pass


def _get_json(key: str, default: Any):
    raw = store.get_setting(key, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _set_json(key: str, value: Any) -> None:
    try:
        store.set_setting(key, json.dumps(value, ensure_ascii=False))
    except Exception:
        pass


def _send(subject: str, text: str, html: str | None = None) -> bool:
    if not _enabled():
        _log("notify.skip", f"{subject}：未配置收件人或 SMTP")
        return False
    ok, msg = mailer.send(_recipient(), subject, text, html=html)
    _log("notify.send" if ok else "notify.fail", f"{subject}：{msg}")
    return ok


# ---------------- low：开始消耗积分 ----------------

def note_paid_start(*, model: str, account: str, cost: float | None,
                    balance: float | None, daily_total: float | None = None,
                    price_factor: float | None = None,
                    choose_reason: str | None = None,
                    free_alternatives: list[str] | None = None,
                    platform: str | None = None) -> bool:
    """low 模式本次请求选了付费模型 → 当天首次发一封提醒。

    返回是否真的发了邮件。去重标记按天滚动，存 settings 表。

    R2.39（用户诉求）：邮件要说清「这次为什么用了付费模型」——
    - `price_factor`：目录倍率（付费池身份的证据，不是「免费吗」的判据）；
    - `choose_reason`：调度器给出的选模原因；
    - `free_alternatives`：本次真免费模型为什么没用上（429/冷却等）；
    - `platform`：账号所在平台（WorkBuddy / Qoder），用户要求写明账号是谁家的。
    开头那句「免费模型不可用，已自动切换付费」是旧叙事——按新口径，
    免费池只认官方真免费模型（hy4flash-free），付费短请求免扣不代表免费，
    所以改成中性的「本次用了付费模型」，不再暗示「免费池没了」。
    """
    today = date.today().isoformat()
    key = f"{_PREFIX}low_paid_sent_on"
    sent_on = store.get_setting(key, "")
    if sent_on == today:
        return False
    account_disp = display_label(account)
    # R2.39b：账号行带平台，一眼看出是谁家的号（WorkBuddy / Qoder）
    plat_disp = (platform or "").strip()
    if plat_disp:
        # 不 import admin（防循环依赖）；这张小表与 admin.PLATFORM_LABELS 保持一致
        plat_disp = {"workbuddy": "WorkBuddy", "qoder": "Qoder"}.get(
            plat_disp.lower(), plat_disp)
    account_row = (f"{plat_disp} · {account_disp}" if plat_disp else account_disp)
    cost_text = f"{float(cost):.2f}" if cost is not None else "未知（价格无观测数据）"
    bal_text = f"{float(balance):.2f}" if balance is not None else "未知"
    total_text = f"{float(daily_total):.2f}" if daily_total is not None else "（今日累计见后台记录页）"
    factor_text = (
        f"x{float(price_factor):g}" if price_factor is not None else "目录未标价"
    )
    reason_text = (choose_reason or "按本次预计扣费择优").strip()
    free_text = (
        "；".join(free_alternatives)
        if free_alternatives else "本次官方免费模型不可用（限流 / 冷却 / 未标定）"
    )
    subject = "[网关] low 开始消耗积分"
    intro = "本次调度选中了一个付费模型，开始消耗账号积分。"
    footnote = "当天首次提醒，后续不再单独发信。付费模型短请求可能免扣，不代表它属于免费池。"
    rows = [
        ("模型", model),
        ("倍率", factor_text),
        ("为何选它", reason_text),
        ("免费模型为何没用上", free_text),
        ("账号", account_row),
        ("本次扣费", f"{cost_text} 积分"),
        ("当前余额", bal_text),
        ("今日累计", total_text),
    ]
    text = (
        intro + "\n\n"
        + f"模型：{model}\n"
        + f"倍率：{factor_text}\n"
        + f"为何选它：{reason_text}\n"
        + f"免费模型为何没用上：{free_text}\n"
        + f"账号：{account_row}\n"
        + f"本次扣费：{cost_text} 积分\n"
        + f"当前余额：{bal_text}\n"
        + f"今日累计：{total_text}\n\n"
        + footnote + "\n"
    )
    # 卡片样式：与登录邮件 / 签到日报逐像素同款（无跳转目标 → 自动省略按钮）
    html = mailer.card_html(
        title="low 开始消耗积分",
        subtitle=f"{date.today().isoformat()} · 本次用了付费模型，非免费池",
        badge="首次提醒",
        rows=rows,
        cta="", link="",
        intro=intro,
        footnote=footnote,
        section_title="本次概况",
        grad="#7A3B12,#A8551B",          # 暖橙：与「花钱」语义匹配，区别于登录绿/测试蓝
        button_color="#A8551B",
        badge_fg="#B45309",
        badge_bg="#FEF3C7",
    )
    ok = _send(subject, text, html=html)
    if ok:
        store.set_setting(key, today)
    return ok


# ---------------- low：某账号免费模型额度用完 / 429 ----------------
# R2.49：与「开始消耗积分」是**独立事件**，各自独立去重。
# 用户明确：额度占用（去重标记）不该吞掉另一类提醒。
_FREE_EXHAUSTED_KEY = _PREFIX + "free_exhausted_sent_on"


def note_free_exhausted(*, account: str, model: str, platform: str | None = None,
                        reset_at: float | None = None,
                        reason: str | None = None) -> bool:
    """某账号的免费模型撞 429 code 6004（额度用完）→ 当天首次发一封提醒。

    触发判据由调用方给出（调度 cooldown 写入 / 请求 error 里出现 6004）。
    本函数只管「提醒 + 去重」，去重按**天 + 账号**滚动：
    - 同一天同一账号只发一次；
    - 不同账号各发一次（A 号用完不影响 B 号的提醒）。
    与 `note_paid_start` 的 `low_paid_sent_on` 完全独立。
    """
    today = date.today().isoformat()
    seen = _get_json(_FREE_EXHAUSTED_KEY, {})
    if not isinstance(seen, dict):
        seen = {}
    last = seen.get(account)
    if isinstance(last, str) and last == today:
        return False

    account_disp = display_label(account)
    plat_disp = (platform or "").strip()
    if plat_disp:
        plat_disp = {"workbuddy": "WorkBuddy", "qoder": "Qoder"}.get(
            plat_disp.lower(), plat_disp)
    account_row = f"{plat_disp} · {account_disp}" if plat_disp else account_disp
    reset_text = ""
    if reset_at:
        try:
            reset_text = datetime.fromtimestamp(reset_at).strftime("%m-%d %H:%M")
        except Exception:
            reset_text = str(reset_at)
    why = (reason or "上游返回 429（code 6004）：使用量超出频率限制").strip()

    subject = "[网关] 免费额度用完"
    intro = "一个账号的免费模型触发了 429 限额，暂时不能继续用免费模型。"
    rows = [
        ("账号", account_row),
        ("模型", model),
        ("原因", why),
        ("重置时间", reset_text or "上游未提供"),
        ("影响", "该账号的免费模型暂时冷却；其他账号 / 付费模型不受影响"),
    ]
    text = intro + "\n\n" + "\n".join(f"{k}：{v}" for k, v in rows) + "\n"
    footnote = (
        "当天该账号只提醒一次；重置后自动恢复。这是「额度用完」提醒，"
        "与「开始消耗积分」是两件事，互不占用提醒名额。"
    )
    html = mailer.card_html(
        title="免费额度用完",
        subtitle=f"{date.today().isoformat()} · 账号免费模型触发 429 限额",
        badge="额度提醒",
        rows=rows,
        cta="", link="",
        intro=intro,
        footnote=footnote,
        section_title="本次概况",
        grad="#854F0B,#B45309",          # 琥珀：需留意但不紧急，区别于扣费的橙
        button_color="#B45309",
        badge_fg="#854F0B",
        badge_bg="#FEF3C7",
    )
    ok = _send(subject, text, html=html)
    if ok:
        seen[account] = today
        _set_json(_FREE_EXHAUSTED_KEY, seen)
    return ok


# ---------------- high：单账号积分将耗尽 ----------------

def _balance_history() -> dict[str, list[list[float]]]:
    return _get_json(_BAL_HISTORY_KEY, {})


def _peak_of(history: dict, account: str, window_days: int) -> float | None:
    """近 N 天该账号的余额峰值。没有历史返回 None（无法判定，不发信）。"""
    cutoff = time.time() - window_days * 86400
    pts = [b for ts, b in history.get(account, []) if ts >= cutoff]
    return max(pts) if pts else None


def check_low_balance(*, account: str, platform: str, balance: float | None) -> bool:
    """high 模式请求结算后检查：余额跌破平台百分比阈值 → 发一封（边沿触发）。

    阈值 = 近 window_days 天该账号余额峰值 × ratio。
    回升到阈值之上会清掉告警标记，下次跌破可再次提醒。
    """
    from .app import router
    try:
        acfg = router.load_config().get("low_balance_alert") or {}
    except Exception:
        acfg = {}
    if not acfg.get("enabled", True):
        return False
    if balance is None:
        return False
    ratio = float(acfg.get("ratio", 0.1) or 0.1)
    window = int(acfg.get("window_days", 7) or 7)

    # 先更新历史（记录这次观测到的余额），再算峰值
    hist = _balance_history()
    pts = hist.setdefault(account, [])
    pts.append([round(time.time(), 3), float(balance)])
    cutoff = time.time() - _MAX_HISTORY_DAYS * 86400
    pts[:] = [p for p in pts if p[0] >= cutoff][-_MAX_POINTS_PER_ACCOUNT:]
    _set_json(_BAL_HISTORY_KEY, hist)

    peak = _peak_of(hist, account, window)
    if peak is None:
        return False  # 没有基线，不猜
    threshold = peak * ratio
    alert_key = f"{_PREFIX}high_low_alerted"
    alerted = _get_json(alert_key, {})
    already = bool(alerted.get(account))

    if balance > threshold:
        # 回升：清标记，下次跌破可再提醒
        if already:
            alerted.pop(account, None)
            _set_json(alert_key, alerted)
        return False
    # 跌破且未告警 → 发信
    if already:
        return False
    subject = "[网关] high 账号积分将尽"
    unit = "credits_qoder" if platform == "qoder" else "credits_workbuddy"
    platform_cn = {"qoder": "Qoder", "workbuddy": "WorkBuddy"}.get(platform, platform)
    intro = "high 模式：账号余额跌破预警阈值。"
    footnote = "余额耗尽会自动换号继续；需继续用本账号请及时充值。"
    account_disp = display_label(account)
    rows = [
        ("账号", account_disp),
        ("平台", platform_cn),
        ("当前余额", _money(balance, unit)),
        (f"近 {window} 日峰值", _money(peak, unit)),
        ("告警阈值", f"{threshold:.2f} {unit}（峰值 × {int(ratio * 100)}%）"),
    ]
    text = (
        intro + "\n\n"
        + f"账号：{account_disp}\n"
        + f"平台：{platform}\n"
        + f"当前余额：{_money(balance, unit)}\n"
        + f"近 {window} 日峰值：{_money(peak, unit)}\n"
        + f"告警阈值：{threshold:.2f} {unit}（峰值 × {int(ratio * 100)}%）\n\n"
        + footnote + "\n"
    )
    html = mailer.card_html(
        title="high 账号积分将尽",
        subtitle=f"{date.today().isoformat()} · {platform_cn} 账号余额跌破阈值",
        badge="余额预警",
        rows=rows,
        cta="", link="",
        intro=intro,
        footnote=footnote,
        section_title="账号概况",
        grad="#7A1F2B,#9F2839",          # 深红：余额告警
        button_color="#9F2839",
        badge_fg="#B91C1C",
        badge_bg="#FEE2E2",
    )
    ok = _send(subject, text, html=html)
    if ok:
        alerted[account] = True
        _set_json(alert_key, alerted)
    return ok


def reset_state() -> None:
    """仅用于测试/手动重置：清掉所有 R1 提醒状态。"""
    for suffix in ("low_paid_sent_on", _BAL_HISTORY_KEY.replace(_PREFIX, ""),
                   "high_low_alerted"):
        store.set_setting(_PREFIX + suffix, "")
