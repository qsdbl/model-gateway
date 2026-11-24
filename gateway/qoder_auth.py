"""Qoder 设备码授权（P44/P45）——在服务器上驱动 qoderclicn 的登录流程。

**两种模式**（P45 补的）：

| 模式 | 场景 | 扫码之后 |
|---|---|---|
| `reauth` | 账号已有但**缺 dt 授权**（账号行的「授权」按钮） | 补齐登录态 + 刷新 dt- |
| `enroll` | **新增账号**（「添加账号 → Qoder」） | 建登录态 + 建凭据 + 落库，**一步到位** |

P44 遗留的坑：授权成功后 `_finish()` 依赖 `load_credential(label)`，
而凭据文件是签到系统**单向复制**过来的 ⇒ 网关侧新增的账号天然没有凭据文件 ⇒
新增流程必然失败。这就是「新增账号还得再点一次授权」的根因。

P45 的解法见 `gateway/qoder_enroll.py`：把 CLI 的 `deviceToken/poll` 请求
引到本地代理，截获响应体里的**明文 dt-/drt-** ⇒ 新增账号也能自己造凭据。

流程（和 WorkBuddy 那套扫码同构，但简单得多——不需要 pty）：

    1. 后台起 `qoderclicn login`，指定 `QODERCN_CONFIG_DIR=<该账号的目录>`
       （新增账号时先用一个临时目录，扫码成功才知道账号叫什么）
    2. CLI 打印一个 `https://qoder.cn/device/selectAccounts?...` 链接
    3. 服务端把链接画成二维码，前端弹出来；用户扫码 / 在浏览器里确认
    4. CLI 拿到 dt-/drt-，写进自己的 `.auth/user`；**代理顺手截下明文 dt-**
    5. 后端落凭据、校验身份（uid 对不对得上）、刷新余额、落库
    6. **扫码完就直接可用了 —— 不需要再去账号 tab 点一次「授权」**

**设备码只有 5 分钟有效期**，用户可能过会儿才点。所以这里做了自动续期：
CLI 报「Device flow timed out」就立刻重开一轮、把新链接推给前端，
用户不需要管过期这件事。

安全边界：
- CLI 的输出**不进网关日志**（里面是一次性 challenge/machine_token）；
- 截获的 dt- **只**写进 0600 凭据文件，不进日志/DB/响应；
- 代理只绑 `127.0.0.1`；
- 二维码在服务端生成，链接不发给任何第三方二维码服务。
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .qrlogin import _qr_svg

# 设备码链接。CLI 会把它折行打印，所以匹配前先把空白去掉。
_URL = re.compile(r"https://qoder\.cn/device/selectAccounts[^\s\"'<>]+")
_TIMEOUT_MARK = "timed out"

# 一轮设备码的有效期（CLI 自己也是 5 分钟）
ROUND_TTL = 280
# 整个会话的存活上限（用户完全不点也不能永远挂着进程）
SESSION_TTL = 3600

# 同时最多跑几个登录进程：每个 CLI 进程常驻 ~250MB，
# 服务器可用内存不到 700MB，跑多了直接 OOM。
MAX_CONCURRENT = max(1, int(os.environ.get("GATEWAY_QODER_AUTH_CONCURRENCY", "2")))


# ---------------------------------------------------------------- 残留进程清理
#
# 服务重启时，正在等授权的 `qoderclicn login` 子进程会被**孤儿化**——
# 它不会跟着网关一起死（PPID 变成 1），于是继续占着 ~250MB 内存。
# 服务器总共才 1.87GB，攒几个就把网关挤死了。
#
# 对策：起进程时把 PID 写进该账号的 config 目录（`.login_pid`），
# 网关启动时扫一遍这些目录，把还活着的 PID 收掉。
def _pid_file(cfg_dir: Path) -> Path:
    return Path(cfg_dir) / ".login_pid"


def _write_pid(cfg_dir: Path, pid: int) -> None:
    try:
        p = _pid_file(cfg_dir)
        p.write_text(str(pid), encoding="utf-8")
        os.chmod(p, 0o600)
    except Exception:
        pass


def _clear_pid(cfg_dir: Path) -> None:
    try:
        p = _pid_file(cfg_dir)
        if p.exists():
            p.unlink()
    except Exception:
        pass


def _kill_pid(pid: int) -> bool:
    """杀一个 PID（先确认还活着，且确实是 qoderclicn）。"""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().decode("utf-8", "replace")
    except Exception:
        return False
    if "qoderclicn" not in cmd:
        return False
    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except Exception:
        return False


def cleanup_orphan_logins() -> int:
    """启动时清理上一轮遗留的登录进程。返回清掉几个。"""
    from . import qoder_upstream as qu
    from .config import PROJECT_ROOT, settings

    base = Path(settings.data_dir).expanduser()
    if not base.is_absolute():
        base = PROJECT_ROOT / base
    root = base / "qoder-cli"
    if not root.is_dir():
        return 0
    killed = 0
    for pf in root.glob("*/.login_pid"):
        try:
            pid = int((pf.read_text(encoding="utf-8") or "0").strip() or 0)
        except Exception:
            pid = 0
        if pid and _kill_pid(pid):
            killed += 1
        try:
            pf.unlink()
        except Exception:
            pass
    return killed


class QoderAuthSession:
    """一个账号的一次授权会话。

    `key` 是会话标识：
    - `reauth` 模式下 = DB label（`qoder:<手机号>`）；
    - `enroll` 模式下 = `new:<uuid>`（此时账号叫什么还不知道）。
    """

    def __init__(self, key: str, label: str = "", mode: str = "reauth") -> None:
        self.key = key
        self.label = label
        self.mode = mode              # enroll | reauth
        self.state = "idle"           # idle | starting | waiting | done | error | cancelled
        self.message = ""
        self.url: str | None = None
        self.username: str | None = None
        self.started_at = 0.0
        self.renewed_at = 0.0
        self.renew_count = 0
        self.poll_error: dict[str, str] | None = None  # 诊断：poll 的真实 errorCode
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._enroll_dir: Path | None = None

    # ---------- 对外 ----------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            out: dict[str, Any] = {
                "key": self.key,
                "label": self.label,
                "mode": self.mode,
                "state": self.state,
                "message": self.message,
                "url": self.url,
                "username": self.username,
                "renew_count": self.renew_count,
                "started_at": self.started_at,
                "expires_in": 0,
                "poll_error": self.poll_error,
            }
        if out["state"] == "waiting":
            out["expires_in"] = max(0, int(ROUND_TTL - (time.time() - self.renewed_at)))
        if out["url"]:
            out["qr"] = _qr_svg(out["url"])
        return out

    def start(self) -> dict[str, Any]:
        if self.state in ("starting", "waiting") and \
                time.time() - self.started_at < SESSION_TTL:
            return self.snapshot()
        self.stop()
        self._stop.clear()
        self.state = "starting"
        self.message = "正在启动 Qoder 登录流程…"
        self.url = None
        self.started_at = time.time()
        self.renewed_at = time.time()
        self.renew_count = 0
        self._thread = threading.Thread(
            target=self._run, name=f"qoder-auth-{self.key}", daemon=True
        )
        self._thread.start()
        return self.snapshot()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        self._proc = None
        if self.state not in ("done",):
            self.state = "cancelled"
            self.message = "已取消"

    # ---------- 内部 ----------

    def _set(self, **kw: Any) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def _config_dir(self) -> Path:
        """这一轮用哪个 config 目录。

        - `reauth`：账号自己的目录（凭据文件里写了 display_label，按它算）；
        - `enroll`：**临时目录** —— 扫码成功前根本不知道账号叫什么。
        """
        from . import qoder_upstream as qu

        if self.mode == "enroll":
            if self._enroll_dir is None:
                base = Path(qu.settings.data_dir).expanduser()
                if not base.is_absolute():
                    base = qu.PROJECT_ROOT / base
                self._enroll_dir = base / "qoder-cli" / f"_enroll_{self.key[-8:]}"
                self._enroll_dir.mkdir(parents=True, exist_ok=True)
            return self._enroll_dir
        return qu.config_dir(self.label)

    def _run(self) -> None:
        """一轮轮的跑登录流程：过期就自动重开。"""
        from . import qoder_upstream as qu
        from .qoder_enroll import EndpointRedirect, PollCapture

        # 并发闸门：每个 CLI 进程 ~250MB，服务器撑不了几个。
        if not _SLOTS.acquire(timeout=30):
            self._set(state="error", message="现在有太多授权流程在跑，稍后再试。")
            return
        try:
            self._run_inner(qu, EndpointRedirect, PollCapture)
        finally:
            _SLOTS.release()

    def _run_inner(self, qu, EndpointRedirect, PollCapture) -> None:
        while not self._stop.is_set():
            if time.time() - self.started_at > SESSION_TTL:
                self._set(state="expired", message="授权会话超过 1 小时未完成，已取消。")
                return
            try:
                cli = qu._cli_path()
            except Exception:
                self._set(state="error",
                          message="服务器上找不到 qoderclicn，Qoder 上游暂时不可用。")
                return

            cfg = self._config_dir()

            # --- 代理 + 端点重定向：为了截获明文 dt- ---
            cap = PollCapture()
            redirect = None
            try:
                port = cap.start()
                redirect = EndpointRedirect(cfg, port)
                redirect.apply()
            except Exception:
                cap.stop()
                cap = None
                redirect = None

            watchdog = None
            wd_stop = threading.Event()
            if redirect is not None:
                watchdog = threading.Thread(
                    target=self._watch_redirect, args=(redirect, wd_stop),
                    name="qoder-endpoint-watchdog", daemon=True,
                )
                watchdog.start()

            # 诊断：CLI 的 stdout 是阻塞读，poll 的 errorCode 只落在 cap 里。
            # 起个轻量轮询把它带到 session 状态，前端/API 能看到真实错误。
            diag_stop = threading.Event()
            diag = threading.Thread(
                target=self._watch_poll_error, args=(cap, diag_stop),
                name="qoder-poll-diag", daemon=True,
            )
            diag.start()

            env = dict(os.environ)
            env["QODERCN_CONFIG_DIR"] = str(cfg)
            env["NO_COLOR"] = "1"
            env.pop("QODER_DEVICE_TOKEN", None)
            try:
                proc = subprocess.Popen(
                    [cli, "login"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, env=env,
                )
            except Exception as e:
                self._set(state="error", message=f"启动登录程序失败：{type(e).__name__}")
                self._cleanup_proxy(cap, redirect)
                return

            self._proc = proc
            _write_pid(cfg, proc.pid)
            url: str | None = None
            buf = ""
            timed_out = False
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    if self._stop.is_set():
                        break
                    text = raw.decode("utf-8", "replace")
                    buf += text
                    flat = re.sub(r"\s+", "", buf)
                    if url is None:
                        m = _URL.search(flat)
                        if m:
                            url = m.group(0)
                            self._set(state="waiting", url=url,
                                      message="请扫码或点链接确认授权",
                                      renewed_at=time.time())
                    if _TIMEOUT_MARK in buf.lower():
                        timed_out = True
                        break
            except Exception:
                pass
            finally:
                try:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
                self._proc = None
                _clear_pid(cfg)
                wd_stop.set()
                diag_stop.set()
                if watchdog is not None:
                    watchdog.join(timeout=2)
                diag.join(timeout=2)
                # 端点一定要还原 —— 还原失败 = 这个账号的推理会打到一个死端口
                self._cleanup_proxy(cap, redirect)

            if self._stop.is_set():
                self._set(state="cancelled", message="已取消")
                return

            # 登录成功：CLI 自己把凭据写进了 .auth/user
            if (cfg / ".auth" / "user").is_file():
                self._finish(cfg, cap)
                return

            if timed_out:
                # 设备码过期（5 分钟）。自动重开一轮，新链接推给前端。
                self.renew_count += 1
                self._set(state="waiting", url=None,
                          message="上一轮授权码已过期，正在自动换一个…",
                          renewed_at=time.time())
                continue

            code = proc.returncode
            self._set(state="error",
                      message=f"登录程序退出（code={code}），没拿到凭据。"
                              f"可以点「重新获取」再来一次。")
            return

    def _watch_redirect(self, redirect, wd_stop: threading.Event) -> None:
        """CLI 偶尔会自己重写 endpoint cache，把重定向顶掉 —— 盯着，掉了就补。"""
        while not wd_stop.is_set():
            try:
                if not redirect.is_applied():
                    redirect.apply()
            except Exception:
                pass
            wd_stop.wait(2.0)

    def _watch_poll_error(self, cap, diag_stop: threading.Event) -> None:
        """把代理观测到的 poll errorCode 搬到 session 状态（诊断用）。"""
        while not diag_stop.is_set():
            try:
                if cap is not None and cap.last_error:
                    self._set(poll_error=dict(cap.last_error))
            except Exception:
                pass
            diag_stop.wait(1.0)

    def _cleanup_proxy(self, cap, redirect) -> None:
        if redirect is not None:
            try:
                redirect.restore()
            except Exception:
                pass
        if cap is not None:
            try:
                cap.stop()
            except Exception:
                pass

    # ---------- 落地 ----------

    def _finish(self, cfg: Path, cap) -> None:
        """登录态有了 —— 落凭据、校验身份、写库、刷余额。

        两条路径在这里合流：
        - `enroll`：新账号，扫码后才知道它叫什么 → 建凭据 + 建账号；
        - `reauth`：老账号，补 dt 授权 → 核对 uid 后刷新凭据。
        """
        from . import qoder_upstream as qu
        from .qoder_enroll import promote_auth_dir, write_credential
        from .store import store

        got = cap.captured if cap is not None else None
        if not got and cap is not None:
            # 正常情况下代理先落盘再把响应回给 CLI，所以这里立刻就有；
            # 万一有微秒级竞态，等一下（最多 3 秒）。
            got = cap.wait(timeout=3.0)
        token = (got or {}).get("token") or ""
        refresh = (got or {}).get("refresh") or ""
        uid = (got or {}).get("uid") or ""
        poll_name = (got or {}).get("username") or ""
        expires_at = (got or {}).get("expires_at") or None
        refresh_expires_at = (got or {}).get("refresh_expires_at") or None

        # 账号名和 uid 以 **CLI 自己的 status** 为准（`Username` + 头像里的 uid），
        # 和现有那 4 个账号的 display_label 是同一套命名；
        # poll 响应里的 user_name / user_id 只作兜底。
        # 代价是多起一个 `qoderclicn status` 进程（~250MB）——
        # 但此刻登录进程**已经退出**，是串行的，内存不吃紧。
        info: dict[str, Any] = {}
        try:
            info = qu.run_cli_status_in(cfg)
        except Exception as e:
            info = {"ok": False, "reason": f"status 异常：{type(e).__name__}"}
        if not info.get("ok"):
            # 登录态不可用 ⇒ 不落地半残账号。
            # 「扫码一次即完整可用」是承诺，落地一个不能推理的账号等于没做。
            self._set(state="error",
                      message=(info.get("reason") or "登录态无效")
                      + "。请点「重新获取二维码」再试一次。")
            return
        username = info.get("username") or poll_name
        uid = info.get("uid") or uid
        if not username:
            self._set(state="error", message="没解析出账号名，无法落地。")
            return

        try:
            if self.mode == "enroll":
                label, cred_path = self._enroll_account(
                    qu, cfg, username, token, refresh, uid,
                    expires_at, refresh_expires_at)
            else:
                label, cred_path = self._refresh_account(
                    qu, info, username, token, refresh, uid,
                    expires_at, refresh_expires_at)
        except Exception as e:
            self._set(state="error", message=f"账号落地失败：{type(e).__name__}: {e}")
            return

        self.username = username
        self.label = label

        # 余额：拿真实值。拿不到就记 NULL，绝不用估算冒充账单（P41 红线）。
        balance = None
        if token:
            try:
                usage = qu.fetch_usage(token)
                balance = usage.get("total")
            except Exception:
                balance = None

        try:
            store.upsert_account(
                label=label, platform="qoder",
                credential_path=str(cred_path),
                auth_status="ok", enabled=True,
            )
            if balance is not None:
                store.update_balance(
                    label, float(balance), "credits_qoder", time.time()
                )
        except Exception:
            pass

        qu.clear_status_cache(label)
        if self.mode == "enroll":
            self._set(state="done",
                      message=f"账号已添加（{self.username}），可直接用于推理。")
        else:
            self._set(state="done",
                      message=f"授权成功（{self.username}），已可作为推理上游。")

    def _enroll_account(self, qu, cfg: Path, username: str, token: str,
                        refresh: str, uid: str, expires_at, refresh_expires_at
                        ) -> tuple[str, Path]:
        """新增账号：定显示名 → 搬目录 → 写凭据。"""
        from .qoder_enroll import promote_auth_dir, write_credential

        display = (username or "").strip()
        if not display:
            raise ValueError("没解析出账号名")
        final = qu.config_dir_for_display(display)
        promote_auth_dir(cfg, final)
        cred_path = write_credential(
            qu._accounts_dir(), display_label=display, token=token,
            refresh=refresh, uid=uid,
            token_expires_at=expires_at, refresh_expires_at=refresh_expires_at,
        )
        return f"qoder:{display}", cred_path

    def _refresh_account(self, qu, info, username: str, token: str,
                         refresh: str, uid: str, expires_at, refresh_expires_at
                         ) -> tuple[str, Path]:
        """补 dt 授权：核对 uid → 刷新凭据里的 dt-。"""
        from . import qoder_upstream as qu2
        from .qoder_enroll import write_credential

        label = self.label
        cred = qu2.load_credential(label)
        expected = cred.get("uid") or ""
        got_uid = (info or {}).get("uid") or uid
        if expected and got_uid and expected != got_uid:
            raise ValueError(
                f"扫码登录的是另一个账号（uid {got_uid[:8]}… ≠ {expected[:8]}…）")
        display = cred.get("display_label") or label
        if token:
            cred_path = write_credential(
                qu2._accounts_dir(), display_label=display,
                token=token, refresh=refresh or cred.get("refresh") or "",
                uid=got_uid or expected,
                campaign_id=cred.get("campaign_id") or "",
                token_expires_at=expires_at or cred.get("expires_at"),
                refresh_expires_at=refresh_expires_at or cred.get("refresh_expires_at"),
            )
            return label, cred_path
        # 没截到明文 dt-（代理没生效）：登录态仍然有效，账号照样能用推理，
        # 只是余额查不了 —— 不阻塞流程。
        return label, qu2._credential_path(label)


# ---------------------------------------------------------------- 会话表

_SESSIONS: dict[str, QoderAuthSession] = {}
_LOCK = threading.Lock()
_SLOTS = threading.Semaphore(MAX_CONCURRENT)


def _session(key: str) -> QoderAuthSession:
    with _LOCK:
        s = _SESSIONS.get(key)
        if s is None:
            s = QoderAuthSession(key)
            _SESSIONS[key] = s
        return s


def start(label: str, mode: str = "reauth") -> dict[str, Any]:
    """发起一次授权。

    - `mode="reauth"` + label：给**已有账号**补 dt 授权（账号行的「授权」按钮）；
    - `mode="enroll"`（label 忽略）：**新增账号**，扫码即完整可用。
    """
    from . import qoder_upstream as qu

    label = (label or "").strip()
    if mode == "enroll":
        key = f"new:{uuid.uuid4().hex[:12]}"
        s = QoderAuthSession(key, label="", mode="enroll")
        with _LOCK:
            _SESSIONS[key] = s
        snap = s.start()
        return {"ok": True, **snap}

    if not label:
        return {"ok": False, "state": "error", "message": "没指定账号"}
    files = qu.list_credential_files()
    if label not in files:
        # 没有凭据文件 → 当成新增账号处理（比报错有用：
        # 用户点的就是「新增/补授权」，不该被一句「没凭据」挡住）
        return start("", mode="enroll")
    # 凭据目录先建好（CLI 会往里写登录态）
    qu.config_dir(label)
    s = _session(label)
    snap = s.start()
    return {"ok": True, **snap}


def status(key: str) -> dict[str, Any]:
    """轮询授权进度。"""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "state": "error", "message": "没指定会话"}
    s = _session(key)
    return {"ok": True, **s.snapshot()}


def cancel(key: str) -> dict[str, Any]:
    key = (key or "").strip()
    if not key:
        return {"ok": False, "message": "没指定会话"}
    s = _session(key)
    s.stop()
    return {"ok": True, "state": "cancelled", "message": "已取消", "key": key}


def active_labels() -> list[str]:
    return [lb for lb, s in _SESSIONS.items() if s.state in ("starting", "waiting")]
