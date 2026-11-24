"""Qoder 真实上游适配器（P44）。

**为什么上游是子进程而不是 URL**：Qoder CN 版的请求签名与响应解密都在 WASM 里，
纯 Python 重写不划算。官方 Python SDK（`qodercn-agent-sdk`）自带一个 183MB 的
`qoderclicn`（node-sea 单文件可执行），服务器不需要 Node。所以这里的"上游"
是一个**子进程**，不是一个 URL。

协议依据（实测确认，不是推测）：

1. **鉴权 —— 只认本地登录态 `<config-dir>/.auth/user`（WASM 加密）。**
   这是全篇最关键、也最容易搞错的一条，踩了两轮：

   | 喂给 CLI 的凭据 | CLI 的反应 |
   |---|---|
   | `{"type":"deviceToken","deviceToken":"dt-..."}` | `Unknown auth payload type: deviceToken` |
   | `{"type":"accessToken","accessToken":"dt-..."}` | `The provided access token was rejected` |
   | `{"type":"qodercli"}`（无本地登录态） | `Not logged in` |
   | 环境变量 `QODER_DEVICE_TOKEN=<dt->` | **完全被忽略**（隔离目录实测：与不给 token 一样报 `Not logged in`） |

   ⇒ 结论：**每个 Qoder 账号必须各自完成一次设备码授权**，CLI 把
   `dt-/drt-` 写进自己的 `<config-dir>/.auth/user`，之后它自己读、自己续期。
   我们从外面**解不开**这个文件（WASM `credential_storage_encrypt`），也不需要解——
   CLI 就是上游，它自己读自己的凭据。

   ⇒ 隔离方式：`QODERCN_CONFIG_DIR=<每账号一个目录>`（CLI 的 `--config-dir`
   是同义选项）。一个账号一份登录态，互不覆盖。

2. **输入格式**（`--input-format stream-json`）是 Anthropic 风格，每行一个消息：
   `{"type":"user","message":{"role":"user","content":[{"type":"text","text":"..."}]}}`
   `assistant` 角色也接受，多轮上下文能带上（实测隔一轮问"我叫什么"能答对）。

3. **输出格式**（`--output-format stream-json`）是 JSONL 事件：
   - `system/init`：会话就绪
   - `stream_event`：只在加 `--include-partial-messages` 时出现，内含
     `content_block_delta` 增量文本 —— 这是流式转发需要的
   - `assistant`：`message.content[].text` 完整文本 + `message.usage.credits`
   - `result`：`total_credits`（**这次真实扣掉的积分**）、`result` 完整文本、
     `subtype` = success / error_during_execution

4. **计费**：上游只给积分，**不给 token 数**（`input_tokens` 恒为 0）。
   所以 usage 里 `credit` 填真实 `total_credits`，
   `prompt_tokens` 记 **0 / NULL** —— P41 红线：拿不到真实值不许用估算冒充账单。
   （估算只用于**排序**，不进账单。）

5. **内存**：单个 CLI 进程常驻约 **250MB**。服务器总内存 1.87GB、可用不到 700MB，
   每个账号各常驻一个必然 OOM。所以并发用信号量限流（默认 2），
   进程用完即退（`--no-session-persistence`，不落会话文件）。

6. **省钱的必要参数**：`--system-prompt ""`。CLI 默认会注入自己的 agent system
   prompt，实测单次扣费从 0.0099 涨到 0.0185（+46%）。

安全边界：
- 凭据只从本地 0600 文件读，token 不进日志/DB/响应/前端。
- 子进程 stdout/stderr 不进网关日志（可能含一次性 state）。
- Qoder 积分与 WorkBuddy 积分**不同计价单位，分别排序、不互比**（router.py 的硬约束）。
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, AsyncIterator

from .config import PROJECT_ROOT, settings

# CLI 二进制位置：优先环境变量，其次 SDK 自带那份，最后 PATH。
DEFAULT_CLI_CANDIDATES = (
    "/tmp/qsdk/.venv/lib/python3.13/site-packages/qodercn_agent_sdk/_bundled/qoderclicn",
)
CLI_BIN = os.environ.get("GATEWAY_QODER_CLI", "").strip()

USAGE_URL = os.environ.get(
    "GATEWAY_QODER_USAGE_URL", "https://openapi.qoder.com.cn/sash/api/v2/me/usage"
)
API_BASE = os.environ.get("GATEWAY_QODER_API", "https://openapi.qoder.com.cn")
USERINFO_URL = API_BASE + "/api/v1/userinfo"

# 并发上限：每个 CLI 进程 ~250MB，服务器可用内存不到 700MB。
MAX_CONCURRENCY = max(1, int(os.environ.get("GATEWAY_QODER_CONCURRENCY", "2")))
_SEM = asyncio.Semaphore(MAX_CONCURRENCY)

# 单次请求超时（CLI 冷启动 ~2-3s + 模型推理）
REQUEST_TIMEOUT = float(os.environ.get("GATEWAY_QODER_TIMEOUT", "180"))

# 登录态检查的缓存时长：跑一次 `qoderclicn status` 要起一个 250MB 的进程，
# 不能每次列账号都跑。5 分钟足够。
AUTH_TTL = float(os.environ.get("GATEWAY_QODER_AUTH_TTL", "300"))

# Qoder CN 的模型表（2026-11-20 由 `--list-models` 实测列出）。
# kind=chat 表示能当聊天模型路由；tools=False 是因为我们用 `--tools ""`
# 关掉了 CLI 自带的 agent 工具（Bash/Read/Write 那些），客户端传的
# tools 字段本版本不接。
MODEL_TABLE: list[dict[str, Any]] = [
    {"model_name": "Auto", "display_name": "Auto（自动选）"},
    {"model_name": "Fast", "display_name": "Fast（快速）"},
    {"model_name": "Qwen4-Max", "display_name": "Qwen4-Max"},
    {"model_name": "Qwen4-Max-Preview", "display_name": "Qwen4-Max-Preview"},
    {"model_name": "Qwen3.8-Max", "display_name": "Qwen3.8-Max"},
    {"model_name": "Qwen3.8-Flash", "display_name": "Qwen3.8-Flash"},
    {"model_name": "DeepSeek-V4-Pro", "display_name": "DeepSeek-V4-Pro"},
    {"model_name": "DeepSeek-Flash", "display_name": "DeepSeek-Flash"},
    {"model_name": "GLM-5.3", "display_name": "GLM-5.3"},
    {"model_name": "GLM-5.3-Flash", "display_name": "GLM-5.3-Flash"},
    {"model_name": "Kimi-K3.1-Pro", "display_name": "Kimi-K3.1-Pro"},
    {"model_name": "Kimi-K2.8-Preview", "display_name": "Kimi-K2.8-Preview"},
    {"model_name": "Step-5-Preview", "display_name": "Step-5-Preview"},
    {"model_name": "MiniMax-M3.1-Flash", "display_name": "MiniMax-M3.1-Flash"},
]

# 当前请求使用的账号（与 workbuddy_upstream 同样的 contextvar 模式：
# 每个请求一份，不会串号 —— P43 踩过的坑，contextvar 设了不读等于没设）。
_ACCOUNT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "qoder_account", default=None
)


class UpstreamError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class CredentialError(UpstreamError):
    pass


# ---------------------------------------------------------------- 凭据


def _accounts_dir() -> Path:
    p = Path(settings.credential_dir).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def list_credential_files() -> dict[str, Path]:
    """凭据目录里的 Qoder 账号文件：{label: path}。

    文件名后缀 `.qoder.json`，和 WorkBuddy 的 `.info` 区分开。
    label 取文件里 `label` 字段（= `qoder:<显示名>`，和 accounts 表里的 label 一致），
    取不到就退回文件名主干。
    """
    d = _accounts_dir()
    if not d.is_dir():
        return {}
    out: dict[str, Path] = {}
    for p in sorted(d.glob("*.qoder.json")):
        if not p.is_file():
            continue
        label = p.stem
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("label"), str):
                label = data["label"]
        except Exception:
            pass
        out[label] = p
    return out


def known_accounts() -> dict[str, Path]:
    return list_credential_files()


def current_account() -> str | None:
    return _ACCOUNT.get()


def use_account(label: str | None):
    return _ACCOUNT.set(label or None)


def reset_account(token) -> None:
    try:
        _ACCOUNT.reset(token)
    except Exception:
        pass


def _credential_path(label: str | None = None) -> Path:
    files = list_credential_files()
    if label:
        if label in files:
            return files[label]
        p = Path(label).expanduser()
        if p.is_file():
            return p
        raise CredentialError(f"qoder credential for account '{label}' not found")
    if files:
        return next(iter(files.values()))
    raise CredentialError("no qoder credential available")


def load_credential(label: str | None = None) -> dict[str, Any]:
    """读账号的签到凭据（dt-/drt-）。

    这份凭据用于**查余额**和**续期**；推理用的是 CLI 自己那份登录态
    （见模块 docstring 第 1 条）。两者是同一批账号的独立 token 实例。
    """
    p = _credential_path(label)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise CredentialError(f"credential file unreadable: {type(e).__name__}")
    if not isinstance(data, dict):
        raise CredentialError("credential file is not an object")
    return {
        "label": label or data.get("label") or p.stem,
        "display_label": data.get("display_label") or "",
        "uid": str(data.get("uid") or ""),
        "token": str(data.get("token") or ""),
        "refresh": str(data.get("refresh") or ""),
        "campaign_id": str(data.get("campaign_id") or ""),
        "expires_at": data.get("token_expires_at"),
        "refresh_expires_at": data.get("refresh_expires_at"),
        "file": p.name,
        "updated_at": p.stat().st_mtime,
    }


def account_meta(label: str | None = None) -> dict[str, Any] | None:
    try:
        cred = load_credential(label)
    except Exception:
        return None
    return {
        "label": cred["label"],
        "file": cred["file"],
        "updated_at": cred["updated_at"],
        "expires_at": cred["expires_at"],
        "refresh_expires_at": cred["refresh_expires_at"],
        "uid": cred["uid"],
        "display_label": cred["display_label"],
        "platform": "qoder",
    }


# ---------------------------------------------------------------- 配置目录

_SAFE_RE = re.compile(r"[^0-9A-Za-z一-鿿_-]+")


def config_dir_for_display(display: str) -> Path:
    """按**显示名**算配置目录（不看凭据文件）。

    新增账号时登录态先落在临时目录，扫码成功才知道显示名 ——
    那时凭据文件还不存在，只能走这条路。
    """
    base = Path(settings.data_dir).expanduser()
    if not base.is_absolute():
        base = PROJECT_ROOT / base
    safe = _SAFE_RE.sub("_", display) or "default"
    d = base / "qoder-cli" / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_dir(label: str) -> Path:
    """该账号的 CLI 配置根。CLI 会把登录态写进 `<这里>/.auth/user`。

    一个账号一个目录 —— 同目录并发会抢实例锁，而且登录态会互相覆盖
    （P44 实测：4 个账号共用一个目录时，全部跑成最后登录的那个）。

    目录名用凭据里的 **display_label**（手机号掩码这种），不是 DB label
    （`qoder:<手机号>`）—— 前者才是 CLI 那边看得懂的名字，
    手工授权时按同一个名字建目录，两边才对得上。
    """
    source = label
    try:
        cred = load_credential(label)
        if cred.get("display_label"):
            source = cred["display_label"]
    except Exception:
        pass
    return config_dir_for_display(source)


def credential_file(label: str) -> Path:
    """CLI 自己的登录态文件（WASM 加密，我们只判断它在不在，不读内容）。"""
    return config_dir(label) / ".auth" / "user"


# ---------------------------------------------------------------- 模型目录


def catalog(ttl: float = 60.0) -> list[dict[str, Any]]:
    """Qoder 模型表。统一成和 workbuddy_upstream.catalog() 一样的字段形状，
    这样调度器那边不用改数据结构。

    `credits_factor` 一律给 None：Qoder 的积分费率**没标定过**，
    目录里的倍率不能拿来排序（P41 的教训：倍率与实际费率不成比例）。
    `is_free` 给 None：免费与否是**长度区间**属性，不是模型属性。
    """
    out = []
    for m in MODEL_TABLE:
        out.append({
            "model_name": m["model_name"],
            "display_name": m["display_name"],
            "kind": "chat",
            "tools": False,
            "image": False,
            "context_window": 128000,
            "credits_factor": None,
            "is_free": None,
            "platform": "qoder",
        })
    return out


def model_info(model_id: str) -> dict[str, Any] | None:
    for m in catalog():
        if m["model_name"] == model_id:
            return m
    return None


def known_models() -> list[str]:
    return [m["model_name"] for m in MODEL_TABLE]


# ---------------------------------------------------------------- 余额 / 身份


def fetch_usage(token: str | None = None) -> dict[str, Any]:
    """调 Qoder 的用量接口，拿剩余积分。

    返回结构：
      {"total": 剩余积分, "used": 已用, "expires_at": 秒级时间戳|None, ...}
    """
    if token is None:
        label = current_account()
        cred = load_credential(label)
        token = cred["token"]
        if not token:
            raise UpstreamError("credential missing token")
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "model-gateway/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise UpstreamError(f"usage api http {e.code}")
    except Exception as e:
        raise UpstreamError(f"usage api failed: {type(e).__name__}")

    q = (data or {}).get("qoderUsage") or {}
    user_q = q.get("userQuota") or {}
    add_q = q.get("addOnQuota") or {}
    total = _num(user_q.get("remaining")) + _num(add_q.get("remaining"))
    used = _num(user_q.get("used")) + _num(add_q.get("used"))
    exp = q.get("expiresAt")
    return {
        "total": total,
        "used": used,
        "quota_total": _num(user_q.get("total")) + _num(add_q.get("total")),
        "is_quota_exceeded": bool(q.get("isQuotaExceeded")),
        "user_type": q.get("userType") or "",
        "expires_at": (exp / 1000.0) if isinstance(exp, (int, float)) and exp > 1e11 else exp,
    }


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_balance(label: str | None = None) -> tuple[float, str, float]:
    """(余额, 单位, 检查时刻)。单位固定 credits_qoder。"""
    usage = fetch_usage()
    return float(usage["total"]), "credits_qoder", time.time()


def fetch_userinfo(token: str) -> dict[str, Any]:
    """拿账号在 Qoder 上的真实身份（id / name / username）。"""
    req = urllib.request.Request(
        USERINFO_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "model-gateway/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        raise UpstreamError(f"userinfo failed: {type(e).__name__}")
    if not isinstance(data, dict):
        raise UpstreamError("userinfo bad payload")
    return data


# ---- 登录态（CLI 本地凭据）检查 ----

_status_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_identity_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_UID_FROM_AVATAR = re.compile(r"/users/([0-9a-fA-F-]{8,})/")


def _parse_status(text: str) -> dict[str, Any]:
    """解析 `qoderclicn status` 的输出。

    实测输出（已授权）：
        Version: 1.1.79
        Username: <nickname>
        Avatar: https://qoder.com.cn/users/<uid>/default/avatars
        User Type: personal_standard
        Login Method: browser
        Auth Source: local

    实测输出（未授权 / 凭据读不出来）：
        Version: 1.1.79
        Account: Not logged in (Qoder CLI temporarily could not read your saved login
                 (MACHINE_ID_MISSING). Retry the previous command. ...)

    ⇒ 后一种要把括号里的原因抠出来，不然页面只能显示「没解析出用户名」，
    用户完全不知道是「还没授权」还是「凭据文件缺了 machine_id」。
    """
    out: dict[str, Any] = {"username": None, "uid": None, "user_type": None,
                           "auth_source": None, "login_method": None,
                           "not_logged_in": False, "reason": ""}
    for line in text.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip().lower()
        v = v.strip()
        if k == "username":
            out["username"] = v or None
        elif k == "avatar":
            m = _UID_FROM_AVATAR.search(v)
            if m:
                out["uid"] = m.group(1)
        elif k == "user type":
            out["user_type"] = v or None
        elif k == "auth source":
            out["auth_source"] = v or None
        elif k == "login method":
            out["login_method"] = v or None
        elif k == "account":
            # 「Account: Not logged in (原因…)」
            if "not logged in" in v.lower():
                out["not_logged_in"] = True
                m = re.search(r"\(([^()]*MACHINE_ID[^()]*|[^()]*)\)\s*$", v)
                out["reason"] = (m.group(1) if m else v).strip()[:160]
            elif v:
                out["username"] = v
    return out


def _cli_path() -> str:
    if CLI_BIN and Path(CLI_BIN).is_file():
        return CLI_BIN
    for cand in DEFAULT_CLI_CANDIDATES:
        if Path(cand).is_file():
            return cand
    found = shutil.which("qoderclicn")
    if found:
        return found
    raise UpstreamError(
        "qoderclicn 未找到（设 GATEWAY_QODER_CLI 指向它，或装 qodercn-agent-sdk）"
    )


def run_cli_status(label: str, timeout: int = 40) -> dict[str, Any]:
    """跑一次 `qoderclicn status`，看这个账号的本地登录态。

    同步调用（要等子进程退出），**只在后台点「检查授权」时用**，
    不放在列表页的常规路径上。
    """
    return run_cli_status_in(config_dir(label), timeout=timeout)


def run_cli_status_in(cfg_dir: Path, timeout: int = 40) -> dict[str, Any]:
    """在**指定目录**里跑 `qoderclicn status`。

    新增账号时登录态先落在临时目录，凭据文件还不存在，
    `config_dir(label)` 算不出这个目录 —— 所以要能直接传目录进来。
    """
    cli = _cli_path()
    env = dict(os.environ)
    env["QODERCN_CONFIG_DIR"] = str(cfg_dir)
    env["NO_COLOR"] = "1"
    env.pop("QODER_DEVICE_TOKEN", None)
    try:
        proc = subprocess.run(
            [cli, "status"], env=env, capture_output=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "status 超时"}
    except Exception as e:
        return {"ok": False, "reason": f"status 失败：{type(e).__name__}"}
    text = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        return {"ok": False, "reason": (err or text).strip()[:200] or "status 非 0 退出"}
    info = _parse_status(text)
    info["ok"] = bool(info.get("username"))
    if not info["ok"]:
        info["reason"] = info.get("reason") or "status 没解析出用户名"
    return info


def clear_status_cache(label: str | None = None) -> None:
    """登录态变了就把缓存清掉（授权成功后要立刻生效）。"""
    if label is None:
        _status_cache.clear()
    else:
        _status_cache.pop(label, None)


def auth_state(label: str, force: bool = False) -> dict[str, Any]:
    """这个账号能不能当推理上游。

    判据 = CLI 的本地登录态存在且有效，且身份和凭据文件对得上。

    身份比对用 **uid**（Qoder 用户 id，不可变）：`status` 的头像 URL 里带着，
    凭据文件里也有（`uid` 字段）。用户名次之（可能改过）。
    """
    now = time.time()
    if not force:
        hit = _status_cache.get(label)
        if hit and now - hit[0] <= AUTH_TTL:
            return hit[1]

    cred_file = credential_file(label)
    if not cred_file.is_file():
        state = {
            "label": label, "authorized": False, "username": None, "uid": None,
            "checked_at": now, "reason": "未授权（还没有登录态）",
            "credential_present": False,
        }
        _status_cache[label] = (now, state)
        return state

    try:
        info = run_cli_status(label)
    except Exception as e:
        info = {"ok": False, "reason": f"status 异常：{type(e).__name__}"}

    expected_uid = ""
    try:
        expected_uid = load_credential(label).get("uid") or ""
    except Exception:
        pass

    got_uid = info.get("uid") or ""
    mismatch = bool(expected_uid and got_uid and expected_uid != got_uid)
    authorized = bool(info.get("ok")) and not mismatch
    reason = ""
    if not info.get("ok"):
        reason = info.get("reason") or "登录态无效"
    elif mismatch:
        reason = f"登录的是另一个账号（uid {got_uid[:8]}… ≠ {expected_uid[:8]}…）"

    state = {
        "label": label,
        "authorized": authorized,
        "username": info.get("username"),
        "uid": got_uid or expected_uid,
        "expected_uid": expected_uid,
        "user_type": info.get("user_type"),
        "checked_at": now,
        "reason": reason,
        "credential_present": True,
    }
    _status_cache[label] = (now, state)
    return state


def expected_identity(label: str, force: bool = False) -> dict[str, Any]:
    """这个账号在 Qoder 上应该是谁（用签到凭据查 userinfo）。"""
    now = time.time()
    if not force:
        hit = _identity_cache.get(label)
        if hit and now - hit[0] <= 600:
            return hit[1]
    out: dict[str, Any] = {"uid": None, "username": None, "error": None}
    try:
        cred = load_credential(label)
        info = fetch_userinfo(cred["token"])
        out = {
            "uid": info.get("id") or cred.get("uid") or "",
            "username": info.get("name") or "",
            "error": None,
        }
    except Exception as e:
        out = {"uid": None, "username": None, "error": f"{type(e).__name__}"}
    _identity_cache[label] = (now, out)
    return out


def probe_account(label: str | None = None) -> dict[str, Any]:
    """探活：登录态 + 余额都查一遍。给 admin 的「检查授权」按钮用。"""
    label = label or current_account()
    if not label:
        return {"ok": False, "reason": "没指定账号"}
    auth = auth_state(label, force=True)
    out: dict[str, Any] = {
        "ok": bool(auth.get("authorized")),
        "label": label,
        "authorized": bool(auth.get("authorized")),
        "username": auth.get("username"),
        "uid": auth.get("uid"),
        "auth_reason": auth.get("reason"),
    }
    try:
        cred = load_credential(label)
        usage = fetch_usage(cred["token"])
        out.update({
            "balance": usage.get("total"),
            "used": usage.get("used"),
            "user_type": usage.get("user_type"),
            "quota_expires_at": usage.get("expires_at"),
            "token_expires_at": cred.get("expires_at"),
        })
    except Exception as e:
        out["balance_error"] = f"{type(e).__name__}"
    return out


# ---------------------------------------------------------------- 请求组装


def _content_text(content: Any) -> str:
    """OpenAI 的 content 可能是字符串，也可能是 [{type:text,text:...}] 数组。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type") == "image_url":
                    parts.append("[image]")
        return "\n".join(p for p in parts if p)
    return ""


def _split_messages(payload: dict[str, Any]) -> tuple[str, list[tuple[str, str]]]:
    """把 OpenAI 的 messages 拆成 (system 文本, [(role, 文本)] 对话轮)。

    `developer` 角色也当 system —— 上游 WorkBuddy 那边因为不接受 developer
    角色报过 11128（见 README P43），这里提前归一，别把同一个坑再踩一遍。
    """
    system_parts: list[str] = []
    turns: list[tuple[str, str]] = []
    for m in payload.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "user")
        text = _content_text(m.get("content"))
        if role in ("system", "developer"):
            if text:
                system_parts.append(text)
        else:
            turns.append((role, text))
    return "\n\n".join(system_parts), turns


_ROLE_TAG = {"user": "用户", "assistant": "助手", "tool": "工具", "function": "函数"}


def _flatten_turns(turns: list[tuple[str, str]]) -> str:
    """把多轮对话压成一段文本，塞进**一条** user 消息。

    为什么不一条一条喂给 CLI：CLI 会对**每条** user 消息都生成一次回复，
    N 轮历史就是 N 次模型调用（多烧 N-1 次积分）。压成一条只花一次。
    """
    if not turns:
        return ""
    out = []
    for role, text in turns:
        tag = _ROLE_TAG.get(role, role)
        out.append(f"[{tag}]：{text}")
    return "\n\n".join(out)


def _build_cli_args(model: str | None, system_text: str) -> list[str]:
    return [
        "--print",
        "--no-session-persistence",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        # 必须给空串：CLI 默认注入自己的 agent system prompt，
        # 实测单次扣费 +46%（0.0099 → 0.0185）。
        "--system-prompt", system_text,
        "--tools", "",
        "--model", model or "Auto",
    ]


def _build_env(label: str) -> dict[str, str]:
    env = dict(os.environ)
    # 每个账号一个配置根：CLI 的登录态存在这里，同目录并发还会抢实例锁。
    env["QODERCN_CONFIG_DIR"] = str(config_dir(label))
    env["NO_COLOR"] = "1"
    # 别让 SDK 模式的残留环境变量把 CLI 拽进另一种启动路径。
    for k in ("QODER_SDK_AUTH_PAYLOAD_FILE", "QODER_AGENT_SDK_ENTRYPOINT",
              "QODER_SDK_ACCESS_TOKEN", "QODER_PERSONAL_ACCESS_TOKEN"):
        env.pop(k, None)
    return env


# ---------------------------------------------------------------- 执行


async def _spawn_cli(label: str, model: str | None, system_text: str, user_text: str):
    cli = _cli_path()
    args = _build_cli_args(model, system_text)
    env = _build_env(label)
    # 输入用 stdin 喂一行 JSON（Anthropic 风格）。不用命令行传 prompt：
    # 命令行会进 ps 输出，内容不该露在进程列表里。
    line = json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": user_text}],
        },
    }, ensure_ascii=False)
    try:
        proc = await asyncio.create_subprocess_exec(
            cli, *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except Exception as e:
        raise UpstreamError(f"spawn qoder cli failed: {type(e).__name__}")
    try:
        proc.stdin.write(line.encode("utf-8") + b"\n")
        await proc.stdin.drain()
        proc.stdin.close()
    except Exception:
        pass
    return proc


def _parse_event(raw: str) -> dict[str, Any] | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _delta_text(event: dict[str, Any]) -> str:
    """从 stream_event 里抠出增量文本。

    形如：
      {"type":"stream_event","event":{"type":"content_block_delta",
        "delta":{"type":"text_delta","text":"你好"}}}
    """
    inner = event.get("event")
    if not isinstance(inner, dict):
        return ""
    if inner.get("type") != "content_block_delta":
        return ""
    delta = inner.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("text"), str):
        return delta["text"]
    return ""


def _assistant_text(event: dict[str, Any]) -> str:
    msg = event.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        c.get("text", "") for c in content
        if isinstance(c, dict) and c.get("type") == "text"
    )


def _event_credits(event: dict[str, Any]) -> float | None:
    """从事件里取真实扣费积分。

    - assistant 事件：`message.usage.credits`
    - result 事件：`total_credits`
    取不到就是 None（未知，不猜 —— P41 红线）。
    """
    if event.get("type") == "result":
        v = event.get("total_credits")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    if event.get("type") == "assistant":
        usage = (event.get("message") or {}).get("usage")
        if isinstance(usage, dict):
            v = usage.get("credits")
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None
    return None


def _sse(obj: dict[str, Any]) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _chunk(model: str, cid: str, created: int, delta: dict[str, Any],
           finish: str | None = None, usage: dict[str, Any] | None = None) -> str:
    body: dict[str, Any] = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        body["usage"] = usage
    return _sse(body)


def _prepare(payload: dict[str, Any]) -> tuple[str, str]:
    system_text, turns = _split_messages(payload)
    user_text = _flatten_turns(turns)
    if not user_text:
        user_text = _content_text(payload.get("prompt") or "") or "(空请求)"
    return system_text, user_text


async def _collect(model: str | None, payload: dict[str, Any],
                   label: str | None) -> tuple[str, float | None, str | None]:
    """跑一次 CLI，把完整回答 / 扣费 / 错误文本收回来。"""
    label = label or current_account() or "default"
    system_text, user_text = _prepare(payload)
    async with _SEM:
        proc = await _spawn_cli(label, model, system_text, user_text)
        text_parts: list[str] = []
        credits: float | None = None
        error: str | None = None
        try:
            async def _read() -> None:
                nonlocal credits, error
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    ev = _parse_event(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
                    if not ev:
                        continue
                    c = _event_credits(ev)
                    if c is not None:
                        credits = c
                    if ev.get("type") == "assistant":
                        t = _assistant_text(ev)
                        if t:
                            text_parts.append(t)
                    elif ev.get("type") == "result":
                        if ev.get("is_error"):
                            errs = ev.get("errors") or []
                            error = str(errs[0]) if errs else (ev.get("terminal_reason") or "upstream error")
                        elif not text_parts and isinstance(ev.get("result"), str):
                            text_parts.append(ev["result"])

            await asyncio.wait_for(_read(), timeout=REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            _kill(proc)
            raise UpstreamError(f"qoder cli timeout after {int(REQUEST_TIMEOUT)}s")
        finally:
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except Exception:
                    _kill(proc)
    if error:
        raise UpstreamError(error)
    return "".join(text_parts), credits, None


def _kill(proc) -> None:
    try:
        proc.kill()
    except Exception:
        pass


# ---------------------------------------------------------------- 对外接口


async def stream_response(payload: dict[str, Any], model: str | None = None) -> AsyncIterator[str]:
    """流式：把 CLI 的增量事件翻译成 OpenAI SSE。

    Qoder 的积分按**请求**扣（不是按 token），所以这里没有逐 token 的 usage；
    最后一帧一次性把 `usage.credit`（真实扣费）带上。
    """
    label = current_account() or "default"
    system_text, user_text = _prepare(payload)

    model_name = model or "Auto"
    cid = "chatcmpl-" + os.urandom(12).hex()
    created = int(time.time())

    async with _SEM:
        proc = await _spawn_cli(label, model, system_text, user_text)
        sent_any = False
        credits: float | None = None
        final_text: list[str] = []
        try:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                ev = _parse_event(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
                if not ev:
                    continue
                c = _event_credits(ev)
                if c is not None:
                    credits = c
                if ev.get("type") == "stream_event":
                    delta = _delta_text(ev)
                    if delta:
                        sent_any = True
                        yield _chunk(model_name, cid, created, {"content": delta})
                elif ev.get("type") == "assistant":
                    t = _assistant_text(ev)
                    if t:
                        final_text.append(t)
                        if not sent_any:
                            # 没有增量帧（部分模型/配置下 CLI 只给完整消息）：
                            # 这时把完整文本作为一帧发出去，别让客户端等空。
                            sent_any = True
                            yield _chunk(model_name, cid, created, {"content": t})
                elif ev.get("type") == "result" and ev.get("is_error"):
                    errs = ev.get("errors") or []
                    reason = str(errs[0]) if errs else (ev.get("terminal_reason") or "upstream error")
                    yield _sse({"error": {"message": reason, "type": "upstream_error"}})
                    yield "data: [DONE]\n\n"
                    return
        except asyncio.TimeoutError:
            _kill(proc)
            yield _sse({"error": {"message": "qoder upstream timeout", "type": "upstream_error"}})
            yield "data: [DONE]\n\n"
            return
        finally:
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except Exception:
                    _kill(proc)

    usage: dict[str, Any] = {}
    if credits is not None:
        usage["credit"] = credits
    # prompt_tokens 记 0：上游不给真实 token 数，宁可空着也不填估算值。
    usage["prompt_tokens"] = 0
    usage["completion_tokens"] = 0
    yield _chunk(model_name, cid, created, {}, finish="stop", usage=usage)
    yield "data: [DONE]\n\n"


async def build_nonstream_response(payload: dict[str, Any], model: str | None = None) -> dict[str, Any]:
    """非流式：跑一次，把结果拼成 OpenAI 的 chat.completion。"""
    label = current_account()
    text, credits, _err = await _collect(model, payload, label)
    model_name = model or "Auto"
    return {
        "id": "chatcmpl-" + os.urandom(12).hex(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            **({"credit": credits} if credits is not None else {}),
        },
    }


def credit_from_usage(usage: Any) -> float | None:
    """从 usage 里取真实扣费积分（和 credit.py 的同名函数同义）。"""
    if not isinstance(usage, dict):
        return None
    v = usage.get("credit")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 授权维护


def refresh_device_token(token: str, refresh: str) -> tuple[str, str] | None:
    """用 drt- 续期 dt-。成功返回 (新 token, 新 refresh)。

    端点是 `/api/v1/deviceToken/refresh`（**不是** `/api/v1/jobToken/refresh`
    ——后者只认 PAT 换出来的 jobToken，对 drt- 恒 400）。签到系统那边
    之前就踩过这个坑，代码注释还写着「这条分支目前走不到」。
    """
    body = json.dumps({"refresh_token": refresh}).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + "/api/v1/deviceToken/refresh",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "model-gateway/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    new_token = data.get("device_token") or data.get("token") or data.get("access_token")
    if not new_token:
        return None
    return str(new_token), str(data.get("refresh_token") or refresh)


def renew_credential(label: str) -> dict[str, Any]:
    """续期这个账号的签到凭据并回写文件（0600）。

    注意：这只续**签到系统那份** dt-（用于查余额）。
    CLI 那份登录态由 CLI 自己续期，不用我们管。
    """
    cred = load_credential(label)
    if not cred.get("refresh"):
        return {"ok": False, "reason": "凭据里没有 refresh token"}
    out = refresh_device_token(cred["token"], cred["refresh"])
    if not out:
        return {"ok": False, "reason": "续期失败（refresh token 可能已过期）"}
    new_token, new_refresh = out
    path = _credential_path(label)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"ok": False, "reason": "凭据文件读不了"}
    data["token"] = new_token
    data["refresh"] = new_refresh
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return {"ok": True, "label": label, "rotated": new_token != cred["token"]}
