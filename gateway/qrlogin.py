"""扫码添加账号（P27）——在服务器上驱动 codebuddy CLI 的登录流程。

用户反馈：「为啥我点击添加按钮只有一个输入框」「你不早说可以扫码吗」。

事实是：平台本身没有给个人开放的「扫码换 token」HTTP 接口，唯一的官方入口是
CLI 的登录命令。它的行为是：

    1. 起一个带 pty 的交互界面，先问「Do you trust the files in this folder?」
    2. 再问「Select login method」（默认 Chinese Site）
    3. 然后打印一个 https://copilot.tencent.com/login?platform=CLI&state=<uuid>
       并进入轮询（Waiting for auth…），**不依赖 localhost 回调**

所以只要把这个 URL 变成二维码给手机扫，扫完 CLI 自己拿到凭据，我们再把
`.codebuddy/local_storage/entry_*.info` 读出来导入即可。全程不需要用户手动
复制任何 token。

设计取舍：
- 每次 start 都用**新的临时 HOME**。因为 HOME 里一旦有登录态，CLI 会直接进会话
  界面，不再打印登录链接；而且凭据不该在临时目录里长期残留。
- pty 只用一条连接、单会话（`_lock` 保护）。同时开两个没有意义，还会抢同一个
  终端尺寸。
- pty 的输出**不进网关日志**：里面含一次性 state，等同一把临时钥匙。
- CLI 二进制路径可配（GATEWAY_CLI_BIN），默认用服务器上已装好的那份。
"""
from __future__ import annotations

import fcntl
import os
import re
import select
import shutil
import signal
import struct
import tempfile
import termios
import threading
import time
from pathlib import Path

# ANSI 转义（颜色、光标移动、括号属性等）
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")
# 登录链接。终端宽度不够时 CLI 会把 URL 折行，所以匹配前要先去掉所有空白。
_URL = re.compile(
    r"https://copilot\.tencent\.com/login\?platform=CLI&state=[0-9a-fA-F-]+&version=[\d.]+"
)
_TRUST = "Do you trust the files"
_LOGIN_METHOD = "Select login method"

DEFAULT_CLI = ""  # 用 GATEWAY_CLI_BIN 指定；空则靠 PATH 查找 codebuddy
TIMEOUT = 300          # 5 分钟没人扫就自己收摊
QR_CELL = 6            # 二维码像素尺寸（segno scale 用）


class QrSession:
    """一次扫码登录会话。生命周期：start() → status() 轮询 → done/expired。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.pid: int | None = None
        self.fd: int | None = None
        self.home: Path | None = None
        self.url: str | None = None
        self.state = "idle"          # idle | starting | waiting | done | expired | error
        self.message = ""
        self.content: str | None = None
        self.label: str | None = None
        self.started_at = 0.0
        self.finished_at = 0.0
        self._baseline: set[str] = set()
        self._sent_trust = False
        self._sent_method = False
        self._method_at = 0.0
        self._retries = 0
        self._buf = ""
        self._reader: threading.Thread | None = None

    # ---------- 对外 ----------
    def start(self) -> dict:
        """起一个会话并返回登录链接。已经有一个在跑就复用它。"""
        if self.state in ("starting", "waiting") and time.time() - self.started_at < TIMEOUT:
            return self.snapshot()

        self.stop()
        self.reset()
        import shutil
        cli = os.environ.get("GATEWAY_CLI_BIN", "").strip() or (shutil.which("codebuddy") or "")
        if not cli or not Path(cli).exists():
            self.state = "error"
            self.message = (
                "服务器上没找到 codebuddy 命令，扫码登录不可用；"
                "请先安装 CLI（见 README 部署步骤），或设 GATEWAY_CLI_BIN 指向它，"
                "也可以改用「粘贴 .info」。"
            )
            return self.snapshot()

        home = Path(tempfile.mkdtemp(prefix="qr-home-", dir=str(self._base_dir())))
        self.home = home
        storage = home / ".codebuddy" / "local_storage"
        storage.mkdir(parents=True, exist_ok=True)
        self._baseline = {p.name for p in storage.glob("*.info")}
        self.started_at = time.time()
        self.state = "starting"

        env = dict(
            os.environ,
            HOME=str(home),
            TERM="xterm-256color",
            LANG="C.UTF-8",
            LC_ALL="C.UTF-8",
            # 不让 CLI 自己去开浏览器
            BROWSER="true",
        )
        workdir = home / "work"
        workdir.mkdir(exist_ok=True)
        try:
            pid, fd = _spawn(cli, str(workdir), env)
        except Exception as exc:  # pragma: no cover - 起不来就是环境问题
            self.state = "error"
            self.message = f"启动登录程序失败：{type(exc).__name__}"
            return self.snapshot()
        self.pid, self.fd = pid, fd
        self._reader = threading.Thread(target=self._pump, name="qrlogin", daemon=True)
        self._reader.start()

        # 不在这里等链接：实测从起进程到打印链接约 10 秒（信任提示 8s + 选站 1s），
        # 同步等会把 HTTP 请求拖住十几秒，手机上看着像卡死。改成立刻返回
        # state=starting，前端轮询 status（URL 一出现就画二维码）。
        self.state = "starting"
        return self.snapshot()

    def status(self) -> dict:
        if self.state in ("starting", "waiting"):
            elapsed = time.time() - self.started_at
            if self.state == "starting" and not self.url and elapsed > 60:
                self.state = "error"
                self.message = "登录程序启动超时，没给出链接；可改用「粘贴 .info」。"
                self.stop(keep_state=True)
            elif elapsed > TIMEOUT:
                self.state = "expired"
                self.message = "二维码超过 5 分钟没被扫描，已自动取消。"
                self.stop(keep_state=True)
        return self.snapshot()

    def snapshot(self) -> dict:
        out = {
            "state": self.state,
            "url": self.url,
            "message": self.message,
            "label": self.label,
            "expires_in": max(0, int(TIMEOUT - (time.time() - self.started_at)))
            if self.state == "waiting" else 0,
        }
        if self.state == "done":
            # 凭据内容只交给已登录的后台页面，由它 POST 回 accounts/import 走同一套校验
            out["content"] = self.content
        if self.url:
            # 二维码由服务端生成：URL 里的 state 等于一次性钥匙，不发给任何第三方
            # 二维码服务。segno 是可选依赖，缺了就只给链接（前端会提示手动打开）。
            out["qr"] = _qr_svg(self.url)
        return out

    def stop(self, keep_state: bool = False) -> None:
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except Exception:
                pass
            try:
                os.waitpid(self.pid, 0)
            except Exception:
                pass
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
        if self.home and self.home.exists():
            shutil.rmtree(self.home, ignore_errors=True)
        pid, fd, home = self.pid, self.fd, self.home
        state, message, content, label = self.state, self.message, self.content, self.label
        self.reset()
        if keep_state:
            self.state, self.message = state, message
            self.content, self.label = content, label

    # ---------- 内部 ----------
    def _base_dir(self) -> Path:
        d = Path(os.environ.get("GATEWAY_DATA_DIR", "data"))
        if not d.is_absolute():
            d = Path(__file__).resolve().parents[1] / d
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _pump(self) -> None:
        """读 pty，自动回答两个菜单，抓登录链接，盯着凭据文件出现。"""
        assert self.fd is not None and self.home is not None
        while True:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.5)
            except Exception:
                break
            if r:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                self._handle(chunk)
            if self._collect():
                break
            if self.state == "expired":
                break
            # 自己看表：超时不能只靠前端轮 status()。用户关掉页面 / 切到别的 tab
            # 就没人轮询了，pty 会一直挂在那里等登录（实测能挂几十分钟），临时 HOME
            # 也跟着留着。收摊的责任必须落在 reader 线程自己身上。
            elapsed = time.time() - self.started_at
            if self.state in ("starting", "waiting"):
                if self.state == "starting" and not self.url and elapsed > 90:
                    self.state = "error"
                    self.message = "登录程序启动超时，没给出链接；可改用「粘贴 .info」。"
                    break
                if elapsed > TIMEOUT:
                    self.state = "expired"
                    self.message = "二维码超过 5 分钟没被扫描，已自动取消。"
                    break
        if not self.started_at:
            # 已经被 cancel()/stop() 收掉了（reset 会把 started_at 归零）。
            # 这里再写状态会把用户看到的「已取消」改成「已过期」，纯属添乱。
            return
        if self.state not in ("done", "error", "expired"):
            self.state = "expired"
            self.message = "登录进程已退出，没有拿到凭据。"
        if self.state != "done":
            # stop() 而不是 _kill_only()：失败/超时的情况下临时 HOME 里没有可用凭据，
            # 留着只是垃圾（每个目录里都有 CLI 的一堆配置）。
            self.stop(keep_state=True)

    def _handle(self, chunk: bytes) -> None:
        text = _ANSI.sub("", chunk.decode("utf-8", "replace")).replace("\r", "")
        self._buf = (self._buf + text)[-6000:]
        flat = re.sub(r"\s+", "", self._buf)
        if not self.url:
            m = _URL.search(flat)
            if m:
                self.url = m.group(0)
                self.state = "waiting"
                return
        # 两个菜单默认选中第一项，回车即可。实测踩过的坑：检测到菜单立刻写 \r
        # 会被吞掉（界面还没准备好接收输入），进程就停在菜单上不动了。
        # 所以先等一下再写；万一还是没前进，5 秒后补一次（最多两次）。
        now = time.time()
        if not self._sent_trust and _TRUST in self._buf:
            self._sent_trust = True
            time.sleep(0.35)
            self._write(b"\r")
            return
        if not self._sent_method and _LOGIN_METHOD in self._buf:
            self._sent_method = True
            self._method_at = now
            time.sleep(0.5)
            self._write(b"\r")
            return
        if (
            self._sent_method
            and not self.url
            and self._retries < 2
            and _LOGIN_METHOD in self._buf
            and now - self._method_at > 5
        ):
            self._retries += 1
            self._method_at = now
            self._write(b"\r")

    def _write(self, data: bytes) -> None:
        if self.fd is None:
            return
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def _credential_glob(self) -> list[Path]:
        """列出 CLI 可能写凭据的所有位置（新/旧版本兼容）。

        血泪（2026-11-21）：CLI 升到 2.185.0 后，扫码成功的凭据不再写到
        `.codebuddy/local_storage/entry_*.info`，而是写到
        `.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info`。
        旧代码只盯前者 → 用户明明扫码成功（临时目录里凭据都在），网关却一直
        `state=waiting` 直到超时，白扫一遍还以为没加上。所以两个路径都扫。
        """
        assert self.home is not None
        candidates = [
            self.home / ".codebuddy" / "local_storage",                        # 老版本
            self.home / ".local" / "share" / "CodeBuddyExtension" / "Data"
            / "Public" / "auth",                                               # 新版本 2.185+
        ]
        files: list[Path] = []
        for directory in candidates:
            try:
                files.extend(directory.glob("*.info"))
            except Exception:
                continue
        try:
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            pass
        return files

    def _collect(self) -> bool:
        """凭据落盘即完成。返回 True 表示会话结束。

        坑：CLI 一启动就会在 local_storage 里写 `entry_*.info`，但大多是**产品
        配置**（`$schema` / `productName` / `endpoint` 那一套），不是账号凭据。
        只按「文件名是新的」判断会在启动后立刻误报成功。所以这里必须用
        credentials._extract 真正解析一遍（要求 account.uid + auth.accessToken），
        解析不过就继续等。
        """
        from .credentials import _extract

        for path in self._credential_glob():
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            try:
                _extract(content)
            except Exception:
                continue          # 产品配置 / 半截文件，不是我们要的
            self.content = content
            self.label = _guess_label(content)
            self.state = "done"
            self.finished_at = time.time()
            self._kill_only()
            return True
        return False

    def _kill_only(self) -> None:
        """只收进程与 fd，保留 home 供读取（home 在 stop() 时才删）。

        必须 waitpid 回收：fork 出来的子进程被 kill 后如果不 wait，会在系统里留
        一堆 `<defunct>` 僵尸，每次扫码加一个。
        """
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except Exception:
                pass
            try:
                os.waitpid(self.pid, 0)
            except Exception:
                pass
            self.pid = None


def _spawn(cli: str, cwd: str, env: dict) -> tuple[int, int]:
    """forkpty + exec，子进程独占一个 pty。

    窗口宽度设成 200 列：CLI 按终端宽度折行打印登录链接，80 列会把 state
    折成两三段，抓出来就是残缺的 URL。
    """
    pid, fd = os.forkpty()
    if pid == 0:  # pragma: no cover - 子进程分支
        try:
            os.chdir(cwd)
            os.execvpe(cli, [cli, "--model", "deepseek-v4.1-flash"], env)
        except BaseException:
            os._exit(127)
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 200, 0, 0))
    except Exception:
        pass
    return pid, fd


def _qr_svg(text: str) -> str | None:
    """把链接画成二维码（内联 SVG）。segno 缺失时返回 None，前端降级成链接。"""
    try:
        import segno  # 纯 Python、无依赖
    except Exception:
        return None
    try:
        qr = segno.make(text, error="m")
        # 二维码必须高对比，所以固定黑底白（不跟随明暗主题），
        # 深色模式下给个白底容器就行。
        # border=4 是 QR 规范要求的静区（quiet zone，4 个模块）；
        # segno 默认只给 2，四周留白不足时手机容易识别失败。
        svg = qr.svg_inline(scale=4, border=4, dark="#000000", light="#FFFFFF")
        return _with_viewbox(svg)
    except Exception:
        return None


_SVG_SIZE_RE = re.compile(r'<svg\b[^>]*?width="(\d+(?:\.\d+)?)"[^>]*?height="(\d+(?:\.\d+)?)"')


def _with_viewbox(svg: str) -> str:
    """给 segno 的 <svg> 补 viewBox。

    segno 的 svg_inline() 只输出 width/height（scale 固定 4 倍），**没有 viewBox**。
    前端用 CSS 把 svg 拉到容器大小（176px 框），没有 viewBox 时浏览器不会缩放内容，
    只会裁掉超出部分——链接越长二维码模块越多、px 越大（200 字符≈260px），
    塞进 158px 的框里就只剩左上角一块，扫不出来。
    补上 viewBox 后 CSS 尺寸即视口，内容按比例缩放。
    """
    if "viewBox=" in svg:
        return svg
    m = _SVG_SIZE_RE.search(svg)
    if not m:
        return svg
    w, h = m.group(1), m.group(2)
    return svg.replace("<svg ", f'<svg viewBox="0 0 {w} {h}" ', 1)


def _guess_label(content: str) -> str | None:
    """从凭据 JSON 里猜账号名，仅用于提示（真正落库由 import 接口决定）。"""
    import json

    try:
        data = json.loads(content)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    # 新版本 CLI 的昵称在 account.nickname 下，老的在顶层 —— 两处都找。
    candidates = [data]
    for container in ("account", "user", "profile"):
        nested = data.get(container)
        if isinstance(nested, dict):
            candidates.append(nested)
    for src in candidates:
        for key in ("nickname", "account", "accountName", "username", "name", "uid", "user_id"):
            val = src.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:40]
    return None


_LOCK = threading.Lock()
_SESSION = QrSession()


def start() -> dict:
    with _LOCK:
        return _SESSION.start()


def status() -> dict:
    with _LOCK:
        return _SESSION.status()


def cancel() -> dict:
    with _LOCK:
        _SESSION.stop()
        return {"state": "idle", "message": "已取消"}
