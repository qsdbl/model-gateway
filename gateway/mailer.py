"""发信（P20）：只做一件事——用 SMTP 把一封纯文本+HTML 邮件发出去。

为什么自己写而不是引第三方：需求就一个「发一次性登录链接」，用标准库 smtplib
足够，少一个依赖就少一处供应链风险；而且服务器上签到项目已经在用同一套 SMTP。

配置全部来自环境变量（见 config.py），**密码绝不出现在任何返回值/日志里**：

    GATEWAY_SMTP_HOST / PORT / USER / PASSWORD / FROM / SSL
    （未配置 GATEWAY_SMTP_* 时自动回落到通用 SMTP_*，方便复用签到项目的账号）

接口约定：`send()` 永远返回 (ok, message)，不抛异常——发信失败不能把登录页打崩。
"""
from __future__ import annotations

import smtplib
import socket
import ssl
from datetime import datetime as _datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

from .config import settings

# SMTP 配置键（存 SQLite settings 表，后台「安全」页可改；环境变量作兜底）。
# 授权码属于敏感值：存库时明文、页面只显示脱敏，API 不回显完整值。
MAIL_KEYS = ("smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from", "smtp_ssl")


def _smtp_cfg() -> dict:
    """汇总当前 SMTP 配置：SQLite 优先，环境变量兜底。

    用函数而非模块级常量，是因为后台改完配置要立即生效，且测试里 store
    可能换成临时库——每次读取保证拿到最新值。
    """
    try:
        from .store import store
        db = {k: store.get_setting("mail_" + k, "") for k in MAIL_KEYS}
    except Exception:  # noqa: BLE001 —— store 未就绪时用环境变量
        db = {}
    cfg = {
        "host": (db.get("smtp_host") or settings.smtp_host or "").strip(),
        "port": int(db.get("smtp_port") or settings.smtp_port or 465),
        "user": (db.get("smtp_user") or settings.smtp_user or "").strip(),
        "password": db.get("smtp_password") or settings.smtp_password or "",
        "from": (db.get("smtp_from") or settings.smtp_from or "").strip(),
        "ssl": (db.get("smtp_ssl") if db.get("smtp_ssl") != "" else ("1" if settings.smtp_ssl else "0")) != "0",
    }
    cfg["from"] = cfg["from"] or cfg["user"]
    return cfg

# 常见邮箱的「用哪个账号发」提示：只在出错时给人看，不含任何密码
HINTS = {
    "qq.com": "QQ 邮箱要用「授权码」而不是登录密码，且需在设置里开启 SMTP 服务。",
    "163.com": "163 邮箱要用客户端授权码，不是登录密码。",
    "126.com": "126 邮箱要用客户端授权码，不是登录密码。",
    "gmail.com": "Gmail 需要「应用专用密码」，且国内服务器常被墙，建议换国内邮箱。",
}


def mail_configured() -> bool:
    c = _smtp_cfg()
    return bool(c["host"] and c["user"] and c["password"])


def mail_status() -> dict:
    """给后台看的发信状态：只说「配了没、用哪个服务器、谁发」，绝不回密码。"""
    c = _smtp_cfg()
    return {
        "configured": mail_configured(),
        "host": c["host"],
        "port": c["port"],
        "user": c["user"],
        "from": c["from"],
        "ssl": c["ssl"],
        "password_set": bool(c["password"]),   # 只回「设没设」，绝不回明文/密文
    }


def _why_failed(exc: Exception) -> str:
    """把 SMTP 异常翻译成一句能照着做的人话。"""
    c = _smtp_cfg()
    user = c["user"] or ""
    domain = user.split("@")[-1].lower() if "@" in user else ""
    hint = HINTS.get(domain, "")
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ("SMTP 认证失败（535）。" + (hint or "请确认账号与授权码是否匹配。"))
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "连接 SMTP 超时：服务器到邮件服务商的 465/587 端口可能被挡住。"
    if isinstance(exc, ssl.SSLError):
        return "TLS 握手失败：端口与加密方式不匹配（465 用 SSL、587 用 STARTTLS）。"
    if isinstance(exc, socket.gaierror):
        return f"解析不到 SMTP 主机名 {c['host'] or '(空)'}。"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "收件地址被对方服务器拒收，检查邮箱是否写错。"
    return f"{type(exc).__name__}: {exc}"


def send(to: str, subject: str, text: str, html: str | None = None) -> tuple[bool, str]:
    """发一封邮件。返回 (成功?, 人话说明)。"""
    c = _smtp_cfg()
    if not mail_configured():
        return False, "服务器没有配置 SMTP（后台「安全」页可填，或用 GATEWAY_SMTP_* 环境变量），发不了邮件。"
    to = (to or "").strip()
    if not to or "@" not in to:
        return False, "收件邮箱没填或格式不对。"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("模型网关", c["from"]))
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    if html:
        msg.attach(MIMEText(html, "html", "utf-8"))

    ctx = ssl.create_default_context()
    try:
        if c["ssl"]:
            with smtplib.SMTP_SSL(c["host"], c["port"],
                                  timeout=20, context=ctx) as s:
                s.login(c["user"], c["password"])
                s.sendmail(c["from"], [to], msg.as_string())
        else:
            with smtplib.SMTP(c["host"], c["port"], timeout=20) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.login(c["user"], c["password"])
                s.sendmail(c["from"], [to], msg.as_string())
    except Exception as exc:            # noqa: BLE001 —— 发信失败一律降级成人话
        return False, _why_failed(exc)
    return True, "已发送"


def _esc(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ── 版式：统一卡片样式（header 渐变 + 卡片 + 明细行 + 尾注，全 inline style）──
# 为什么这次是「抄」而不是「照着做」：之前两版都是我自己另起一套（table 布局、
# 灰底页、kicker 小标签），在真实收件箱里和签到日报并排一看就不一样。既然那封
# 用户天天看、认可，就把它那 6 个数值级别的东西原样搬过来：
#   · 外层 max-width:600px + margin:0 auto
#   · header: linear-gradient(135deg,grad) / padding:24px / border-radius:12px 12px 0 0
#   · 标题 20px #fff margin:0；副标题 13px rgba(255,255,255,0.88) margin:8px 0 0
#   · 卡片: 1px solid #e5e7eb + border-top:none + padding:24px + 0 0 12px 12px
#   · 行: padding:14px 0 + border-bottom:1px solid #f3f4f6，标题 14px/600 #111827，
#        明细 12px #6b7280 line-height:1.7
#   · 尾注: 12px #9ca3af line-height:1.7 margin:18px 0 0
# 一律 inline style、不引 @media、不套 table 外壳——与那封保持逐像素同款。
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Courier New',monospace"


def card_html(
    *,
    title: str,
    subtitle: str,
    badge: str,
    rows: list[tuple[str, str]],
    cta: str,
    link: str,
    footnote: str,
    intro: str = "",
    credential: str = "",
    copy_link: str = "",
    grad: str = "#1F4D3F,#2A6455",
    button_color: str = "#1F4D3F",
    badge_fg: str = "#1F4D3F",
    badge_bg: str = "#E1F5EE",
    section_title: str = "申请信息",
    rows_style: str = "kv",
) -> str:
    """卡片邮件版式（与签到日报同一套视觉）。

    `credential` 非空时在最上方渲染独立的等宽口令框：`user-select:all` 让长按一次
    直接全选；`copy_link` 非空时框右侧再给一个「复制」按钮，点开是一个**只在前端
    读 `location.hash`** 的小页面（口令放在 URL 的 `#` 之后，不会进服务器日志、
    也不会进 Referer），在那里用剪贴板 API 一键拷走。

    `link` 为空时进入「通知模式」：不渲染按钮和底部地址（网关提醒这类邮件没有
    跳转目标），但整套 header/卡片/明细行的视觉与登录邮件、签到日报完全一致。
    `section_title` 可改明细区标题（通知邮件传「本次概况」之类更贴切的名字）。
    `rows_style="table"` 时每行走「平台 chip + 名字 + 右侧徽章 + 下方指标」的富行
    （与签到日报逐像素同款），此时 rows 每项为 (chip, name, badge, badge_fg, badge_bg, metrics)；
    默认 "kv" 是原来的灰标签 + 正文，老邮件零影响。
    """
    if rows_style == "table":
        # R2.23：签到日报式富行——每个账号一块：上行 chip+名字+右侧徽章，
        # 下行「｜」分隔的指标。样式常量与 daily_report.render_rows 一致。
        detail = ""
        for row in rows:
            chip, name, badge_txt, b_fg, b_bg, metrics = row
            chip_bg, chip_fg = ("#E6F1FB", "#185FA5") if chip == "WorkBuddy" else ("#EEEDFE", "#534AB7")
            detail += (
                '<div style="padding:14px 0;border-bottom:1px solid #f3f4f6;">'
                '<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;">'
                '<div style="min-width:0;">'
                f'<span style="display:inline-block;padding:1px 8px;border-radius:4px;font-size:11px;'
                f'font-weight:600;background:{chip_bg};color:{chip_fg};margin-right:6px;">{_esc(chip)}</span>'
                f'<span style="font-size:14px;font-weight:600;color:#111827;word-break:break-all;">{_esc(name)}</span>'
                '</div>'
                f'<span style="display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;'
                f'font-weight:600;background:{b_bg};color:{b_fg};white-space:nowrap;">{_esc(badge_txt)}</span>'
                '</div>'
                f'<div style="font-size:12px;color:#6b7280;margin-top:6px;line-height:1.7;">{_esc(metrics)}</div>'
                '</div>'
            )
    else:
        # 键值明细：一条一行，灰标签 + 深一点的正文，和签到日报的明细行同字号同色
        detail = "".join(
            '<div style="font-size:12px;color:#6b7280;margin-top:6px;line-height:1.7;">'
            f'<span style="color:#9ca3af;">{_esc(k)}</span>&nbsp;&nbsp;'
            f'<span style="color:#374151;word-break:break-all;">{_esc(v)}</span></div>'
            for k, v in rows
        )

    copy_btn = ""
    if credential and copy_link:
        copy_btn = (
            f'<a href="{_esc(copy_link)}" style="flex:none;display:inline-block;'
            f'padding:7px 14px;border-radius:8px;background:{button_color};color:#ffffff;'
            'font-size:13px;line-height:18px;font-weight:600;text-decoration:none;'
            'white-space:nowrap;">复制</a>'
        )

    cred_block = ""
    if credential:
        cred_block = f"""
        <div style="padding:0 0 16px;border-bottom:1px solid #f3f4f6;">
            <div style="font-size:12px;color:#6b7280;line-height:1.7;margin-bottom:8px;">访问口令</div>
            <div style="background:#f3f4f6;border:1px solid #e5e7eb;border-radius:8px;padding:14px 16px;">
                <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;">
                    <span style="min-width:0;font-family:{MONO};font-size:17px;line-height:24px;
                          color:#111827;word-break:break-all;-webkit-user-select:all;
                          user-select:all;">{_esc(credential)}</span>
                    {copy_btn}
                </div>
            </div>
            <div style="font-size:12px;color:#9ca3af;line-height:1.7;margin-top:8px;">
                长按可全选 · 别转发这封邮件
            </div>
        </div>"""

    # 只在真有话说时才占一行：空字符串不渲染空 <p>（排版不留白）
    intro_block = (
        f'<p style="font-size:12px;color:#6b7280;line-height:1.7;margin:16px 0 0;">{_esc(intro)}</p>'
        if intro else ""
    )
    footnote_block = (
        f'<p style="font-size:12px;color:#9ca3af;line-height:1.7;margin:14px 0 0;">{_esc(footnote)}</p>'
        if footnote else ""
    )

    # CTA 区：没有跳转目标（通知类邮件）就不渲染按钮，也不留底部裸链接
    if link:
        cta_block = f"""
            <div style="padding:18px 0 0;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
                    <td align="center" bgcolor="{button_color}" style="border-radius:10px;">
                        <a href="{_esc(link)}" style="display:block;padding:13px 24px;font-size:15px;
                           line-height:20px;font-weight:600;color:#ffffff;text-decoration:none;
                           border-radius:10px;">{_esc(cta)}</a>
                    </td>
                </tr></table>
            </div>"""
        link_block = f"""
            <p style="font-size:12px;color:#9ca3af;line-height:1.7;margin:6px 0 0;">
                按钮失效时用这个地址：<br>
                <span style="color:#1F4D3F;word-break:break-all;">{_esc(link)}</span>
            </p>"""
    else:
        cta_block = ""
        link_block = ""

    return f"""
    <div style="max-width:600px;margin:0 auto;font-family:{FONT};">
        <div style="background:linear-gradient(135deg,{grad});padding:24px;border-radius:12px 12px 0 0;">
            <h2 style="color:#fff;margin:0;font-size:20px;">{_esc(title)}</h2>
            <p style="color:rgba(255,255,255,0.88);margin:8px 0 0;font-size:13px;">{_esc(subtitle)}</p>
        </div>
        <div style="border:1px solid #e5e7eb;border-top:none;padding:24px;border-radius:0 0 12px 12px;">
            {cred_block}
            <div style="padding:14px 0;border-bottom:1px solid #f3f4f6;">
                <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;">
                    <div style="min-width:0;">
                        <span style="display:inline-block;padding:1px 8px;border-radius:4px;font-size:11px;
                              font-weight:600;background:#E6F1FB;color:#185FA5;margin-right:6px;">模型网关</span>
                        <span style="font-size:14px;font-weight:600;color:#111827;">{_esc(section_title)}</span>
                    </div>
                    <span style="display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;
                          font-weight:600;background:{badge_bg};color:{badge_fg};
                          white-space:nowrap;">{_esc(badge)}</span>
                </div>
                {detail}
            </div>
            {cta_block}
            {intro_block}{footnote_block}
            {link_block}
        </div>
    </div>"""



def magic_mail(
    link: str, *, minutes: int, ip: str, ua: str, test: bool = False,
    token: str = "", copy_link: str = "",
) -> tuple[str, str, str]:
    """一次性登录邮件的三段内容：主题 / 纯文本 / HTML。

    `token` 非空时正文顶部给出**独立的访问口令区**（用户要求：邮件里要有口令，
    能长按或一键复制）。口令是长期凭据，所以框下面明确写「别转发」。
    `copy_link` 是「复制」按钮的目标：带 `#t=<口令>` 的页面，片段不进服务器日志。
    """
    minutes = max(0, int(minutes))
    subject = "模型网关 · 测试邮件" if test else "模型网关 · 后台登录"
    ttl_text = f"{minutes} 分钟内有效，且只能用一次" if minutes else "只能用一次"
    # 副标题只说「哪天 + 什么事」，其余信息正文里都有
    today = _datetime.now().strftime("%Y-%m-%d")
    subtitle = f"{today} · " + ("测试邮件" if test else "一次性登录链接")
    text = (
        ("这是一封测试邮件。\n\n" if test else "")
        + ("访问口令：\n\n"
           f"{token}\n\n----------\n\n" if token else "")
        + "打开后台（不需要再输口令）：\n\n"
        f"{link}\n\n"
        + (f"链接 {ttl_text}。\n" if minutes else "")
        + "非本人操作可忽略。\n\n"
        f"申请来源：{ip}\n"
    )
    # 有效期已经在右上角徽标上，这里不再重复一遍
    rows = [("使用次数", "只能用一次")]
    if test:
        rows.append(("用途", "SMTP 连通性测试"))
    rows.append(("申请来源", ip or "本机"))
    html = card_html(
        title="测试邮件" if test else "后台登录",
        subtitle=subtitle,
        badge="测试" if test else (f"{minutes} 分钟内有效" if minutes else "一次性"),
        rows=rows,
        cta="打开管理后台",
        link=link,
        footnote="",
        credential=token,
        copy_link=copy_link,
        # 测试邮件用蓝色，和自己点的找回邮件一眼区分（与签到日报的成功绿同一套色阶）
        grad="#1E3A5F,#2C527F" if test else "#1F4D3F,#2A6455",
        button_color="#1E3A5F" if test else "#1F4D3F",
        badge_fg="#185FA5" if test else "#1F4D3F",
        badge_bg="#E6F1FB" if test else "#E1F5EE",
    )
    return subject, text, html
