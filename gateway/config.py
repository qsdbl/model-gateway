"""配置层：全部走环境变量，默认相对项目根，本地与服务器只是环境变量不同。

双部署设计（README 第 2.1 节）要求：零硬编码路径、端口/监听可配。
"""
from __future__ import annotations
import re

import os
from pathlib import Path

# 项目根 = 本文件(gateway/config.py)的上两级
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_path(name: str, default: str) -> Path:
    """环境变量指定路径；相对路径按项目根解析。"""
    raw = os.environ.get(name, default)
    p = Path(raw).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


_GPT5_ALIAS_RE = re.compile(r"^gpt[-_]?5(?:\.\d+)?$", re.IGNORECASE)


class Settings:
    """运行时配置。所有值在实例化时从环境读取一次。"""

    def __init__(self) -> None:
        # 监听：默认只回环，符合安全红线；服务器部署改环境变量 + HTTPS 反代
        self.host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
        self.port = int(os.environ.get("GATEWAY_PORT", "8080"))

        # 数据目录：默认项目根下的 data/，可用 GATEWAY_DATA_DIR 指向任意位置
        self.data_dir = _env_path("GATEWAY_DATA_DIR", "data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / os.environ.get("GATEWAY_DB_NAME", "gateway.db")

        # 对外 API Key：客户端调用本网关用；为空表示不校验（仅本地开发）
        self.api_key = os.environ.get("GATEWAY_API_KEY", "")

        # 管理后台访问令牌（P9）：令牌只从 URL/Cookie/Header 传入，未配置则整站 fail-safe。
        # 未配置时后台整站 503（fail-safe，避免公网裸奔）；令牌只从 URL/Cookie/Header 传入。
        #
        # P17 起它升级为「登录口令」：只在登录那一刻用一次，随后换成本地会话 cookie，
        # 地址栏不再出现明文令牌；会话滑动续期，长期不动自动失效。
        self.admin_token = os.environ.get("GATEWAY_ADMIN_TOKEN", "")

        # 后台会话（P17 → P20 放宽）：空闲多久失效（秒）与绝对上限。
        # 用户反馈「12 小时太快、长时间不访问就进不去」：改成 30 天不活动才失效、
        # 绝对上限 180 天。日常使用基本等于一次登录长期有效，但仍然是可撤销的会话。
        self.admin_session_ttl = int(os.environ.get("GATEWAY_ADMIN_SESSION_TTL", "2592000"))   # 30d
        self.admin_session_max = int(os.environ.get("GATEWAY_ADMIN_SESSION_MAX", "15552000"))  # 180d
        # 登录失败限流：同一 IP 在窗口内失败多少次后临时封禁（秒）
        self.admin_fail_limit = int(os.environ.get("GATEWAY_ADMIN_FAIL_LIMIT", "10"))
        self.admin_fail_window = int(os.environ.get("GATEWAY_ADMIN_FAIL_WINDOW", "86400"))  # 24h
        self.admin_block_ttl = int(os.environ.get("GATEWAY_ADMIN_BLOCK_TTL", "86400"))      # 24h

        # 多账号凭据目录（P19）：每个账号一个 <label>.info（0600），可热导入，不用重启。
        self.credential_dir = _env_path("GATEWAY_WB_CREDENTIAL_DIR", "credentials")

        # 邮件找回（P20）：会话过期/换设备时，点一下「发到邮箱」拿一次性登录链接。
        # 收件邮箱在后台「安全」页可改（存库）；这里的 env 只是初始默认值。
        self.admin_email = os.environ.get("GATEWAY_ADMIN_EMAIL", "").strip()
        self.admin_magic_ttl = int(os.environ.get("GATEWAY_ADMIN_MAGIC_TTL", "900"))     # 链接 15 分钟
        self.admin_magic_limit = int(os.environ.get("GATEWAY_ADMIN_MAGIC_LIMIT", "3"))   # 窗口内最多 3 封
        self.admin_magic_window = int(os.environ.get("GATEWAY_ADMIN_MAGIC_WINDOW", "3600"))
        # SMTP：优先 GATEWAY_SMTP_*，回落到通用 SMTP_*（与签到项目同一套账号即可复用）
        self.smtp_host = (os.environ.get("GATEWAY_SMTP_HOST") or os.environ.get("SMTP_HOST", "")).strip()
        self.smtp_port = int(os.environ.get("GATEWAY_SMTP_PORT") or os.environ.get("SMTP_PORT") or 465)
        self.smtp_user = (os.environ.get("GATEWAY_SMTP_USER") or os.environ.get("SMTP_USER", "")).strip()
        self.smtp_password = os.environ.get("GATEWAY_SMTP_PASSWORD") or os.environ.get("SMTP_PASSWORD", "")
        self.smtp_from = (os.environ.get("GATEWAY_SMTP_FROM") or self.smtp_user).strip()
        self.smtp_ssl = (os.environ.get("GATEWAY_SMTP_SSL", "1") != "0")

        # Cloudflare quick tunnel 的本地 metrics 端口（P20）：用来告诉后台「当前 HTTPS 地址」。
        # 手机 App（iOS ATS / Android 9+ 默认禁明文）必须走 HTTPS，直连 http://裸IP 会被拦。
        self.tunnel_metrics = os.environ.get("GATEWAY_TUNNEL_METRICS", "http://127.0.0.1:20241").rstrip("/")

        # 上游选择（P10）：mock=本地测试（默认）；workbuddy=真实账号（仅服务器配凭据时启用）
        self.upstream = os.environ.get("GATEWAY_UPSTREAM", "mock").strip().lower()

        # 对外模型名（R1 双模式）：low = 免费优先的简单任务；high = 指定强模型、愿意花积分。
        # 两个名字直接出现在客户端的 `model` 字段里，由请求决定本次用哪个模式，不是全局开关。
        self.low_model = os.environ.get("GATEWAY_LOW_MODEL", "low")
        self.high_model = os.environ.get("GATEWAY_HIGH_MODEL", "high")
        # public_model 作为兼容别名保留给尚未改动的模块（值 = low），
        # 对外真正的模型列表由 app.list_models 返回 [low, high]。
        self.public_model = self.low_model

        # 对外根地址（P16）：直连 IP 部署时是 http://IP/gw，隧道部署时留空按请求头推断。
        # 只用于后台「接入」页生成可复制的配置块，不参与路由。
        self.public_base = os.environ.get("GATEWAY_PUBLIC_BASE", "").rstrip("/")

        # 固定 HTTPS 入口（P22）：<IP>.sslip.io + nginx 443 + Let's Encrypt。
        # 留空则自动探测（云元数据取公网 IP → 本机 443 做 TLS 握手验证证书）。
        # 配了这个就不用再依赖 cloudflared quick tunnel（那个重启就换域名）。
        self.public_https = os.environ.get("GATEWAY_PUBLIC_HTTPS", "").rstrip("/")
        # 自动探测时拼在域名后的子路径前缀（nginx 把 /gw/ 反代到本服务）
        self.public_prefix = os.environ.get("GATEWAY_PUBLIC_PREFIX", "/gw").rstrip("/")

        # 上游请求超时（秒）
        self.upstream_timeout = float(os.environ.get("GATEWAY_UPSTREAM_TIMEOUT", "60"))

        # 默认上下文窗口；None=不限制（mock 阶段）。P5 按实际模型目录覆盖。
        _cw = os.environ.get("GATEWAY_CONTEXT_WINDOW", "").strip()
        self.context_window = int(_cw) if _cw else None

    def canonical_model(self, model_name: str | None) -> str | None:
        """把客户端模型名归一到已发布的对外模型名；未知名原样返回（由 mode_of 判 404）。

        有些客户端（如 bot 创建流程）会自己填 `gpt-5` / `gpt-5.4` 这类具体版本名，
        它们并不在网关对外发布的两个模型名里。这类统一归一到 low，别直接 404。
        """
        if not model_name:
            return model_name
        name = model_name.strip()
        if _GPT5_ALIAS_RE.match(name):
            return self.low_model
        return name

    def mode_of(self, model_name: str | None) -> str:
        """把客户端传来的对外模型名归一到模式；未知名返回空串（调用方按 404 处理）。"""
        name = self.canonical_model(model_name)
        if name == self.high_model:
            return "high"
        if name == self.low_model:
            return "low"
        return ""

    def published_models(self) -> list[str]:
        return [self.low_model, self.high_model]

    def __repr__(self) -> str:  # 不打印 api_key
        return (
            f"Settings(host={self.host}, port={self.port}, db={self.db_path}, "
            f"models=[{self.low_model}, {self.high_model}], timeout={self.upstream_timeout}, "
            f"api_key_set={bool(self.api_key)}, admin_token_set={bool(self.admin_token)})"
        )


settings = Settings()
