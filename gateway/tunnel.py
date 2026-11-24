"""对外地址发现（P20 隧道 → P22 固定 HTTPS 入口）。

为什么需要它：手机 App（iOS ATS、Android 9+ cleartext 策略）默认**禁止明文 HTTP**，
直连 `http://<裸 IP>/gw/v1` 会在网络层直接失败（Chatbox 报 `Network Error: Load failed`），
而服务端其实是通的。所以配置块里必须给出一个 HTTPS 地址。

两种来源，**优先级从高到低**：

1. **固定 HTTPS（P22，推荐）**：`<公网IP>.sslip.io` 这类通配 DNS 永远解析到本机 IP，
   配 nginx 443 + Let's Encrypt 证书即可。不依赖任何第三方隧道进程，不会「重启就换
   域名」，手机端也不经过 Cloudflare（国内运营商对 trycloudflare.com 有阻断）。
   地址来源依次是：`GATEWAY_PUBLIC_HTTPS` env → 自动探测（阿里云元数据取公网 IP，
   再用本机 443 做一次带 SNI 的 TLS 握手，证书不对就不认）。

2. **Cloudflare quick tunnel（备用）**：`gateway-tunnel.service` 把公网 HTTPS 反代到
   127.0.0.1:8080。域名是**进程级随机**的，cloudflared 一重启就换新的（服务器不重启
   也会因为 Restart=always 而变），无法指定、也无法预测下一个名字。cloudflared 自带
   metrics 端口，`GET /quicktunnel` 能拿到当前域名，后台据此实时显示。
"""
from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.request

from .config import settings

_cache: dict = {"at": 0.0, "url": ""}
_TTL = 60.0          # 域名基本不变，60 秒缓存足够；失败了也缓存，别每次刷新都卡 2 秒


# ----------------------------------------------------------------------
# P22 固定 HTTPS 入口
# ----------------------------------------------------------------------
_fixed: dict = {"at": 0.0, "url": ""}
_FIXED_TTL = 300.0   # 固定地址几乎不变，5 分钟缓存


def _sslip_host(ip: str) -> str:
    """把公网 IP 映射到 `<IP>.sslip.io`（sslip.io 的通配 DNS 规则）。"""
    return ip.replace(".", "-") + ".sslip.io"


def _public_ip() -> str:
    """取本机公网 IP：先问云厂商元数据（内网地址，快且不产生出网流量）。"""
    for url in (
        "http://100.100.100.200/latest/meta-data/eipv4",       # 阿里云 EIP
        "http://100.100.100.200/latest/meta-data/public-ipv4",  # 阿里云公网 IP
    ):
        try:
            with urllib.request.urlopen(url, timeout=0.8) as resp:  # noqa: S310
                ip = resp.read().decode("utf-8", "replace").strip()
            if ip.count(".") == 3 and all(p.isdigit() for p in ip.split(".")):
                return ip
        except Exception:
            continue
    return ""


def _tls_ok(host: str, port: int = 443) -> bool:
    """本机 443 上有没有为该域名签发的**可信**证书（TLS 握手 + 证书校验都过）。"""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.2) as raw:
            with ctx.wrap_socket(raw, server_hostname=host):
                return True
    except Exception:
        return False


def fixed_https_base(force: bool = False) -> str:
    """固定 HTTPS 根地址（含子路径前缀）；不可用时返回空串。"""
    now = time.time()
    if not force and now - _fixed["at"] < _FIXED_TTL:
        return _fixed["url"]

    url = ""
    explicit = (getattr(settings, "public_https", "") or "").strip().rstrip("/")
    if explicit:
        url = explicit
    else:
        ip = _public_ip()
        if ip:
            host = _sslip_host(ip)
            if _tls_ok(host):
                prefix = (settings.public_prefix or "").rstrip("/")
                url = f"https://{host}{prefix}"
    _fixed.update(at=now, url=url)
    return url


def quick_tunnel_url(force: bool = False) -> str:
    """当前 quick tunnel 的 https 根地址；没开隧道/取不到返回空串。"""
    now = time.time()
    if not force and _cache["url"] is not None and now - _cache["at"] < _TTL:
        return _cache["url"]
    url = ""
    try:
        with urllib.request.urlopen(  # noqa: S310 —— 只连本机 metrics 端口
            f"{settings.tunnel_metrics}/quicktunnel", timeout=1.5
        ) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        host = str(data.get("hostname") or "").strip()
        if host:
            url = f"https://{host}"
    except Exception:
        url = ""
    _cache.update(at=now, url=url)
    return url
