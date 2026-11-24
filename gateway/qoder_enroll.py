"""Qoder 设备码「扫码一次即完整可用」的落地支持（P45）。

## 为什么需要这个模块

Qoder 的 **dt- / drt- 明文**只在一个瞬间出现：设备码 `poll` 成功的那一下，
响应体里是 `{"token": "dt-...", "refresh_token": "drt-..."}`。

- CLI 会把它**加密**存进 `<config-dir>/.auth/user`（WASM 里的
  `credential_storage_encrypt`），我们读不出来（P44 试过十几种 AES-GCM 密钥候选，
  全部失败）；
- 签到系统里那份 dt- 是从**本机 Mac 桌面端** `auth.v1.dat` 导出来的，
  **网关侧新增账号时根本拿不到** —— 这正是「新增账号还得再点一次授权」的根因。

## 解法

让 CLI 的 `poll` 请求**经过我们**：

1. `deviceToken/poll` 只需要 `nonce + verifier + challenge_method`，
   **不需要请求签名**（这点与其余 CN 推理接口不同，那些走 `withWasmSigning`）。
   逆向出来的调用是 `GET {openapi}/api/v1/deviceToken/poll?...`。
2. 所以把 CLI 的 openapi 端点临时指向本地代理 → 代理原样转发给真实服务器 →
   **响应体里就有明文 dt-**，我们顺手截下来。
3. 流程结束**必须把端点还原**，否则该账号后续的推理请求会打到一个
   已经关掉的本地端口，账号直接失效。

端点重定向只能靠改 CLI 的 endpoint cache：
`QODERCN_VPC_OPENAPI_ENDPOINT` 环境变量实测**无效**（CLI 仍走 openapi 域名）。

## 安全边界

- 代理只绑 `127.0.0.1`，不监听外网；
- 截获的 dt- **只**写进 0600 的凭据文件，**不进日志、不进 DB**（红线）；
- 代理日志默认关闭（`GATEWAY_QODER_ENROLL_DEBUG=1` 才开），且即便开了
  也会把 token / verifier / machine_token 抹掉。
"""
from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

# poll 端点（逆向自 CLI 的 startDeviceFlow）
POLL_PATH = "/api/v1/deviceToken/poll"

# 真实 openapi。CLI 自带的 endpoint cache 里写的是 gateway.qoder.com.cn，
# 两个域名指向同一套后端（实测 poll 经 gateway 域名也能通）。
# 2026-11-22 A/B 实测：换 gateway 域名后 poll 404 依旧（errorCode=NotFound 不变），
# 且无人扫码时的 404 本就是 device flow 的正常 pending 态 ⇒ 域名不是根因。
DEFAULT_UPSTREAM = "https://openapi.qoder.com.cn"

# endpoint cache 里需要改的字段
_CACHE_FILES = ("endpoint-cache-cn.json", "qoder-client-endpoint-cache-cn.json")

# CLI 认的完整端点条目（实测：只写 openapi 那一个字段它不认，会把整个缓存
# 当成无效然后自己重新解析 → 重定向直接失效，poll 根本不走我们这里）。
_GATEWAY = "https://gateway.qoder.com.cn"
_DEFAULT_PROD: dict[str, Any] = {
    "fastEndpoint": _GATEWAY,
    "fastEndpoints": [_GATEWAY],
    "endpoint": _GATEWAY,
    "inferEndpoints": [_GATEWAY],
    "securityEndpoint": _GATEWAY,
    "securityEndpoints": [_GATEWAY],
    "centerEndpoint": _GATEWAY,
    "centerEndpoints": [_GATEWAY],
}
_DEFAULT_SET_NAMES = ("center", "inference", "securityInference", "openapi")

_DEBUG = os.environ.get("GATEWAY_QODER_ENROLL_DEBUG", "") not in ("", "0", "false")

# 文件名里不能有的字符（路径分隔符、NUL、控制字符）。
_BAD_FILENAME = re.compile(r"[/\\\x00-\x1f]")


def _safe_filename(name: str) -> str:
    """显示名 → 文件名主干。只挡真正会破坏路径的字符，
    其余原样保留（手机号掩码原样透传）。"""
    cleaned = _BAD_FILENAME.sub("_", (name or "").strip())
    cleaned = cleaned.lstrip(".") or "qoder"
    return cleaned


def _log(msg: str) -> None:
    if _DEBUG:
        print(f"[qoder-enroll] {msg}", flush=True)


def _redact(text: str) -> str:
    """把 URL 里的敏感参数抹掉，只留形状。"""
    out = []
    for part in text.split("&"):
        key = part.split("=", 1)[0]
        if key in ("verifier", "machine_token", "nonce", "challenge"):
            out.append(f"{key}=<RED>")
        else:
            out.append(part)
    return "&".join(out)


# ==================================================================== 代理


class PollCapture:
    """本地 HTTP 代理：转发 CLI 的 openapi 请求，截获 poll 响应里的明文 token。"""

    def __init__(self, upstream: str = DEFAULT_UPSTREAM) -> None:
        self.upstream = (upstream or DEFAULT_UPSTREAM).rstrip("/")
        self._srv: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._got: dict[str, Any] | None = None
        self._ev = threading.Event()
        self.port = 0
        # poll 请求被看到的次数（诊断用：链路有没有通，看这个数）
        self.hits = 0
        # 最近一次 poll 的错误码（诊断用：区分 pending vs 参数不匹配）。
        # 只存 errorCode/errorMessage 这类无害字段，绝不碰 token/verifier。
        self.last_error: dict[str, str] | None = None

    # ---------- 生命周期 ----------

    def start(self) -> int:
        cap = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                pass

            def _proxy(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                req = urllib.request.Request(
                    cap.upstream + self.path, data=body, method=self.command
                )
                for k, v in self.headers.items():
                    lk = k.lower()
                    if lk in ("host", "content-length", "connection", "accept-encoding"):
                        continue
                    req.add_header(k, v)
                try:
                    with urllib.request.urlopen(req, timeout=30) as r:
                        data = r.read()
                        code = r.status
                        ctype = r.headers.get("Content-Type", "application/json")
                except urllib.error.HTTPError as e:
                    try:
                        data = e.read()
                    except Exception:
                        data = b""
                    code = e.code
                    ctype = e.headers.get("Content-Type", "application/json")
                except Exception as e:  # noqa: BLE001
                    _log(f"转发失败 {self.path} {type(e).__name__}")
                    data = b'{"errorCode":"GatewayProxyError"}'
                    code = 502
                    ctype = "application/json"

                if self.path.startswith(POLL_PATH):
                    with cap._lock:
                        cap.hits += 1
                    cap._observe(data)
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except Exception:
                    pass

            do_GET = _proxy
            do_POST = _proxy
            do_PUT = _proxy
            do_PATCH = _proxy
            do_DELETE = _proxy
            do_HEAD = _proxy

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._srv.daemon_threads = True
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(
            target=self._srv.serve_forever, name="qoder-poll-proxy", daemon=True
        )
        self._thread.start()
        _log(f"代理已启动 127.0.0.1:{self.port} -> {self.upstream}")
        return self.port

    def stop(self) -> None:
        srv, self._srv = self._srv, None
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass
        self._thread = None

    # ---------- 截获 ----------

    def _observe(self, data: bytes) -> None:
        """poll 的响应体里有明文 dt- —— 这是整个方案的关键。

        字段来自 CLI 自己的解析器（逆向自 `identity.device.poll`）：

            token:                  dt-...
            refresh_token:          drt-...
            user_id / userId / uid: Qoder 用户 id
            user_name / userName / name: 账号名
            expires_at / expiresAt: dt 过期时间
            refresh_token_expires_at: drt 过期时间

        ⇒ 连 `qoderclicn status` 都不用再起一次（250MB 的进程），
        账号名和 uid 直接从 poll 响应里拿。
        """
        try:
            obj = json.loads(data.decode("utf-8", "replace"))
        except Exception:
            return
        if not isinstance(obj, dict):
            return

        # 诊断：poll 的 4xx 响应体里会带 errorCode（authorization_pending /
        # 6004 之类）。记下来才能区分「用户没点确认」和「参数不匹配导致
        # 服务端拒绝」。只保留无害的错误字段，token/verifier 一律不碰。
        err = obj.get("errorCode") or obj.get("error_code") or obj.get("code")
        if err is not None:
            with self._lock:
                self.last_error = {
                    "errorCode": str(err),
                    "message": str(obj.get("errorMessage")
                                   or obj.get("error_message")
                                   or obj.get("message") or "")[:200],
                }
            _log(f"poll errorCode={err} msg={self.last_error['message'][:80]}")

        token = obj.get("token")
        if not (isinstance(token, str) and token.strip()):
            return

        def pick(*keys: str) -> str:
            for k in keys:
                v = obj.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
                if isinstance(v, (int, float)) and v:
                    return str(v)
            return ""

        with self._lock:
            self._got = {
                "token": token.strip(),
                "refresh": pick("refresh_token", "refreshToken"),
                "uid": pick("user_id", "userId", "uid"),
                "username": pick("user_name", "userName", "name"),
                "expires_at": pick("expires_at", "expiresAt"),
                "refresh_expires_at": pick(
                    "refresh_token_expires_at", "refreshTokenExpiresAt"),
                "raw_keys": sorted(obj.keys()),
            }
        self._ev.set()
        _log(f"截获到 device token（前缀 {token[:3]}…，长度 {len(token)}）")

    def wait(self, timeout: float = 30.0) -> dict[str, Any] | None:
        """等 poll 成功。返回 {token, refresh, raw_keys} 或 None。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._ev.wait(timeout=0.5):
                break
        with self._lock:
            return dict(self._got) if self._got else None

    @property
    def captured(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._got) if self._got else None


# ==================================================================== 端点重定向


class EndpointRedirect:
    """把 CLI 的 openapi 端点临时指向本地代理，结束后还原。

    只能改 CLI 的 endpoint cache —— `QODERCN_VPC_OPENAPI_ENDPOINT` 实测无效。

    ⚠️ 还原必须可靠：还原失败 = 该账号的推理请求打到一个死端口 = 账号静默失效。
    所以还原失败时宁可**删掉**缓存文件，让 CLI 自己重新解析。
    """

    def __init__(self, cfg_dir: Path, port: int) -> None:
        self.cfg_dir = Path(cfg_dir)
        self.port = port
        self.target = f"http://127.0.0.1:{port}"
        self._backup: dict[str, bytes | None] = {}
        self._applied = False

    # ---------- 写 ----------

    def apply(self) -> None:
        cache = self.cfg_dir / ".cache"
        cache.mkdir(parents=True, exist_ok=True)
        for name in _CACHE_FILES:
            path = cache / name
            if name not in self._backup:
                self._backup[name] = path.read_bytes() if path.is_file() else None
            data = self._rewrite(path, name)
            try:
                path.write_bytes(data)
            except Exception:
                pass
        self._applied = True

    def _rewrite(self, path: Path, name: str) -> bytes:
        """把缓存里的 openapi 端点换成代理地址，其余字段原样保留。

        ⚠️ 两个坑（都是实测踩出来的，不是猜的）：

        1. 缺的字段要**补全**，不能只留 openapi 那一个 —— CLI 会校验条目结构，
           不全就当缓存无效、自己重新解析，重定向直接失效。
        2. **`updatedAt` 必须放在 `prod` 里面**，不能放顶层。放顶层时 CLI 判定
           缓存无效，poll 直接打去 openapi.qoder.com.cn（代理一次都收不到）。
        """
        obj: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    obj = loaded
            except Exception:
                obj = {}
        entries = obj.get("entries")
        if not isinstance(entries, dict):
            entries = {}
        prod = entries.get("prod")
        if not isinstance(prod, dict):
            prod = {}

        if name == "endpoint-cache-cn.json":
            for k, v in _DEFAULT_PROD.items():
                prod.setdefault(k, v)
            prod["openapiEndpoint"] = self.target
            prod["openapiEndpoints"] = [self.target]
            entries["prod"] = prod
            obj.setdefault("version", 1)
            obj["entries"] = entries
        else:
            sets = prod.get("endpointSets")
            if not isinstance(sets, dict):
                sets = {}
            for key in _DEFAULT_SET_NAMES:
                cur = sets.get(key)
                if not isinstance(cur, dict):
                    cur = {}
                cur.setdefault("candidates", [_GATEWAY])
                cur.setdefault("selected", _GATEWAY)
                sets[key] = cur
            sets["openapi"]["candidates"] = [self.target]
            sets["openapi"]["selected"] = self.target
            prod["endpointSets"] = sets
            entries["prod"] = prod
            obj.setdefault("version", 2)
            obj["entries"] = entries
        # ⚠️ updatedAt 在 prod 里面（顶层那份 CLI 不认）
        prod["updatedAt"] = int(time.time() * 1000)
        return json.dumps(obj, ensure_ascii=False).encode("utf-8")

    def is_applied(self) -> bool:
        """缓存还是不是指向我们的代理。CLI 偶尔会自己重写缓存，得盯着。"""
        if not self._applied:
            return False
        path = self.cfg_dir / ".cache" / _CACHE_FILES[0]
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            prod = (obj.get("entries") or {}).get("prod") or {}
            return prod.get("openapiEndpoint") == self.target
        except Exception:
            return False

    # ---------- 还原 ----------

    def restore(self) -> bool:
        """还原缓存。返回是否成功。"""
        ok = True
        cache = self.cfg_dir / ".cache"
        for name, original in self._backup.items():
            path = cache / name
            try:
                if original is None:
                    # 原本没有这个文件 → 删掉，让 CLI 重新解析
                    if path.is_file():
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(original)
            except Exception:
                ok = False
        self._applied = False
        return ok


# ==================================================================== 凭据落盘


def write_credential(
    cred_dir: Path,
    *,
    display_label: str,
    token: str,
    refresh: str = "",
    uid: str = "",
    campaign_id: str = "",
    token_expires_at: Any = None,
    refresh_expires_at: Any = None,
) -> Path:
    """写账号凭据文件（0600）。

    文件名用 `display_label`（和签到系统同步过来的那批一致），
    文件里的 `label` 带平台前缀 —— DB 里 label 是 UNIQUE 的，
    不加前缀会把 WorkBuddy 那行覆盖掉（P41 的坑）。
    """
    cred_dir = Path(cred_dir)
    cred_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(cred_dir, 0o700)
    except Exception:
        pass
    payload = {
        "platform": "qoder",
        "kind": "signin",
        "label": f"qoder:{display_label}",
        "display_label": display_label,
        "uid": uid,
        "token": token,
        "refresh": refresh,
        "campaign_id": campaign_id,
        "token_expires_at": token_expires_at,
        "refresh_expires_at": refresh_expires_at,
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    target = cred_dir / f"{_safe_filename(display_label)}.qoder.json"
    fd, tmp = tempfile.mkstemp(dir=str(cred_dir), prefix=".qoder-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass
    return target


def promote_auth_dir(src_dir: Path, dst_dir: Path) -> Path:
    """把登录态目录搬到最终位置。

    - 目标不存在 → 整个目录改名过去（含 logs/.cache，无所谓）；
    - 目标已存在（同名账号之前授权过）→ 只把 `.auth/` 换掉，其余丢弃。
      `.auth/` 必须整个换：登录态绑 machine_id，只拷 `.auth/user` 会报
      `MACHINE_ID_MISSING`（P44 实测）。
    """
    src_dir, dst_dir = Path(src_dir), Path(dst_dir)
    if not dst_dir.exists():
        dst_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_dir), str(dst_dir))
        return dst_dir
    auth_src = src_dir / ".auth"
    auth_dst = dst_dir / ".auth"
    if auth_src.is_dir():
        if auth_dst.exists():
            shutil.rmtree(auth_dst, ignore_errors=True)
        shutil.move(str(auth_src), str(auth_dst))
    shutil.rmtree(src_dir, ignore_errors=True)
    return dst_dir
