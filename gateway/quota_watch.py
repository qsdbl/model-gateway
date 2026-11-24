"""额度政策监控（R2.21）：每天只读采集一次 + 一封汇总邮件（政策变化在信里标注）。

背景（用户原话）：「每小时检查优惠政策，是 1000 积分还是 0？活动结束了呢？签到
每天领 100 还是 0？」用户后续澄清（关键）：

  - **可用积分会被推理请求实时扣掉**，拿它做 0→有/有→0 提醒全是网关自己花钱的噪音；
  - **签到领积分的变化** `daily_report` 每天 12:45 的日报早就在算增量，不必重复；
  - 真正值得盯的是**政策信号**（天级慢变化），且**每天汇总一封即可**。

因此本模块口径（用户 2026-11-23 拍板「每天一次」）：
  - 每天固定窗口采一次：只读官方 usage / 活动状态，不爬 HTML、不触发 claim。
  - 只检三类**政策变化**：签到活动上/下线、账户档位 user_type 变化、额度档位
    quota_total 变化。**绝不拿 available（会消耗的可用积分）做变化检测**。
  - 不发即时提醒邮件。政策变化**写在当天那封汇总邮件里**醒目标注。
  - 每天只发一封汇总；发失败留待下一轮重试（state 里 `_done` 没写就不算完成）。
  - `available=None`（查不到）记 NULL，绝不当成 0 参与任何判定。
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any, Callable

from . import daily_report, mailer, signin
from .config import PROJECT_ROOT, settings
from .display import display_label

SUMMARY_HOUR = 9           # 每天 09:00 后那轮采集并汇总（与签到 12:45 错开）
STATE_KEEP_DAYS = 60

# 政策变化类型 → (中文名, 是否坏消息)
POLICY_KINDS = {
    "claim_on": ("签到活动上线（恢复每天可领 100）", False),
    "claim_off": ("签到活动结束（今后每天领 0）", True),
    "user_type": ("账户档位变化", None),
    "quota_up": ("额度档位上调", False),
    "quota_down": ("额度档位下调", True),
}


# ---------------------------------------------------------------- state（汇总一天一次）
def _state_path() -> Path:
    d = Path(settings.data_dir).expanduser()
    d = d if d.is_absolute() else PROJECT_ROOT / d
    return d / "quota_watch_state.json"


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


# ---------------------------------------------------------------- 只读采集
def _collect_one(platform: str, label: str, path: Path) -> dict[str, Any]:
    """只读地拿一个账号的额度/活动状态。任何失败都标 None，绝不用 0 冒充。"""
    out: dict[str, Any] = {
        "label": label, "name": display_label(label), "platform": platform,
        "available": None, "quota_total": None, "user_type": None,
        "exceeded": None, "claim_active": None, "error": "",
    }
    cred = signin._load_json(path)
    try:
        if platform == "Qoder":
            token = cred.get("token") or ""
            if not token:
                out["error"] = "凭据缺 token"
                return out
            usage = signin.qoder_usage(token) or {}
            # 用 token 直连官方 usage（不是 contextvar 账号），拿全 quota_total/userType
            try:
                from . import qoder_upstream as qu
                full = qu.fetch_usage(token)
                out["available"] = _num_or_none(full.get("total"))
                out["quota_total"] = _num_or_none(full.get("quota_total"))
                out["exceeded"] = bool(full["is_quota_exceeded"]) if full.get("is_quota_exceeded") is not None else None
                out["user_type"] = full.get("user_type") or None
            except Exception as exc:  # noqa: BLE001
                plan, addon = usage.get("plan_remaining"), usage.get("addon_remaining")
                if plan is not None or addon is not None:
                    out["available"] = float(plan or 0) + float(addon or 0)
                out["error"] = f"usage 补充字段失败：{type(exc).__name__}"
            camp = signin.qoder_campaign_status(token)
            out["claim_active"] = camp.get("active")
        else:
            out["available"] = signin.wb_available(cred)
            st = signin.wb_status(cred)
            if st.get("ok"):
                act = st.get("active")
                out["claim_active"] = bool(act) if act is not None else None
            else:
                out["error"] = "状态查询失败"
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}"
    return out


def _num_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def collect() -> list[dict[str, Any]]:
    return [_collect_one(plat, label, path)
            for plat, label, path in daily_report._accounts()]


# ---------------------------------------------------------------- 政策变化检测（不含 available 消耗）
def detect_changes(cur: dict[str, Any], prev: dict[str, Any] | None) -> list[dict[str, Any]]:
    """只检政策信号：**绝不用 available**（会被推理消耗，不是政策）。None 不参与判定。"""
    if not prev:
        return []
    out: list[dict[str, Any]] = []

    def add(kind: str, text: str) -> None:
        out.append({"kind": kind, "text": text})

    # 账户档位
    t_now, t_old = cur.get("user_type"), prev.get("user_type")
    if t_now and t_old and t_now != t_old:
        add("user_type", f"账户档位 {t_old} → {t_now}")

    # 签到活动上/下线（用户最关心：每天领 100 还是 0）
    c_now, c_old = cur.get("claim_active"), prev.get("claim_active")
    if c_now is not None and c_old is not None and c_now != c_old:
        add("claim_on" if c_now else "claim_off", POLICY_KINDS["claim_on" if c_now else "claim_off"][0])

    # 额度档位（Qoder 有；0 档=没试用，>0 档=有额度）。注意这是 total 不是 available。
    q_now, q_old = cur.get("quota_total"), prev.get("quota_total")
    if q_now is not None and q_old is not None and q_now != q_old:
        if q_old <= 0 < q_now:
            add("quota_up", f"额度档位 0 → {_fmt(q_now)}")
        elif q_now <= 0 < q_old:
            add("quota_down", f"额度档位 {_fmt(q_old)} → 0")
        else:
            add("quota_up", f"额度档位 {_fmt(q_old)} → {_fmt(q_now)}")
    return out


def _fmt(v: float | None) -> str:
    if v is None:
        return "-"
    n = float(v)
    return "%d" % n if n == int(n) else "%.2f" % n


# ---------------------------------------------------------------- 汇总邮件（变化写在里面）
def _summary_mail(snaps: list[dict[str, Any]],
                  changes: list[dict[str, Any]], today: str) -> str:
    """每天一封：政策变化置于顶部醒目标注，其后是各账号当前额度概况。"""
    # R2.23：参考每日签到日报的行结构——每账号一块：平台 chip + 名字 + 右侧状态徽章
    # + 下方「｜」分隔指标。政策变化也用同样的富行，放在最上面醒目标注。
    rows: list[tuple[str, str, str, str, str, str]] = []
    has_bad = any(POLICY_KINDS.get(c["kind"], (None, None))[1] is True for c in changes)
    has_good = any(POLICY_KINDS.get(c["kind"], (None, None))[1] is False for c in changes)

    for c in changes:
        rows.append(("政策", c["name"], "变化", "#A32D2D", "#FEE2E2", c["text"]))

    for s in snaps:
        avail = _fmt(s.get("available"))
        act = s.get("claim_active")
        if act is True:
            badge, b_fg, b_bg = "每天可领 100", "#0F6E56", "#E1F5EE"
        elif act is False:
            badge, b_fg, b_bg = "已结束", "#A32D2D", "#FEE2E2"
        else:
            badge, b_fg, b_bg = "未知", "#854F0B", "#FEF3C7"
        ut = s.get("user_type") or "-"
        metrics = f"可用 {avail} ｜ 档位 {ut}"
        rows.append((s["platform"], s["name"], badge, b_fg, b_bg, metrics))

    if has_bad:
        grad, badge_fg, badge_bg, badge = "#6E2A2A,#8C3838", "#A32D2D", "#FEE2E2", "有变化"
    elif has_good:
        grad, badge_fg, badge_bg, badge = "#1F4D3F,#2A6455", "#0F6E56", "#E1F5EE", "有变化"
    else:
        grad, badge_fg, badge_bg, badge = "#1F4D3F,#2A6455", "#1F4D3F", "#E1F5EE", "快照"

    subtitle = f"{today} · 共 {len(snaps)} 个账号"
    if changes:
        subtitle += f" · {len(changes)} 项政策变化"
    return mailer.card_html(
        title="额度与政策汇总",
        subtitle=subtitle,
        badge=badge,
        rows=rows,
        cta="", link="",
        footnote="只列政策信号（活动/档位/额度档位）；可用积分会被请求消耗，不纳入变化提醒。签到积分变化见每日签到日报。",
        section_title="政策变化与当前额度" if changes else "当前额度",
        grad=grad, badge_fg=badge_fg, badge_bg=badge_bg,
        rows_style="table",
    )


# ---------------------------------------------------------------- 主流程
def run(now: datetime.datetime | None = None, *,
        send: bool | Callable[[str, str], bool] | None = None,
        force: bool = False) -> dict[str, Any]:
    """每天一次：只读采集 → 写快照 → 检政策变化 → 发一封汇总。

    `send` 可注入（测试用）；默认走 `daily_report.send_mail`。
    `force=True` 无视当天是否已发（后台「发测试汇总」用）。
    当天已发且非 force 时直接跳过，不重复采集/发信。返回统计，不含凭据。
    """
    from .store import store

    now = now or datetime.datetime.now()
    today = now.date().isoformat()
    state = _load_state()
    today_state = state.get("summary") or {}
    if not force and today_state.get(today) == "done":
        return {"ok": True, "skipped": True, "reason": "今日已汇总", "total": 0,
                "changed": 0, "summary_sent": False, "snapshots": []}

    send_fn = daily_report.send_mail if send is None else (
        daily_report.send_mail if send is True else (lambda *_a: False) if send is False else send)

    snaps = collect()
    changes: list[dict[str, Any]] = []
    for s in snaps:
        prev = store.latest_quota(s["label"])
        store.record_quota_snapshot(
            s["label"], s["platform"],
            available=s["available"], quota_total=s["quota_total"],
            user_type=s["user_type"], exceeded=s["exceeded"],
            claim_active=s["claim_active"], ts=now.timestamp(),
        )
        for ch in detect_changes(s, prev):
            changes.append({"label": s["label"], "name": s["name"],
                            "kind": ch["kind"], "text": ch["text"]})

    summary_sent = False
    if send_fn:
        body = _summary_mail(snaps, changes, today)
        summary_sent = bool(send_fn(f"【额度汇总】{today} · 政策与各账号当前额度", body))
    # 没发出去就不写 done，下一轮还能重试（签到日报 `_done` 同款策略）
    if summary_sent:
        today_state[today] = "done"
    for old in sorted(today_state)[:-STATE_KEEP_DAYS]:
        today_state.pop(old, None)
    state["summary"] = today_state
    _save_state(state)

    return {
        "ok": True, "skipped": False,
        "total": len(snaps), "changed": len(changes), "summary_sent": summary_sent,
        "changes": changes,
        "snapshots": [{k: s.get(k) for k in
                       ("label", "platform", "available", "user_type", "claim_active", "error")}
                      for s in snaps],
    }
