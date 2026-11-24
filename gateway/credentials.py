"""账号凭据导入（P19）。

用户在手机上拿到 WorkBuddy 的 `.info` 凭据内容后，不必再 ssh 到服务器改 .env、
不用重启服务：后台「账号 → 添加账号」贴进来即可。

安全约束（与 README 红线一致）：
- 只写服务器本地文件（0600，目录 0700），**绝不**进 SQLite、日志、响应体；
- 导入前先拿这份凭据真跑一次只读接口（余额查询）验证，错的直接拒绝，
  不把坏凭据留在磁盘上；
- 返回给前端的只有 label / 昵称 / 有效期时间戳，不回显 token。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .config import settings


class ImportError_(Exception):
    """导入失败（消息面向用户，可直接显示在后台）。"""


def _dir() -> Path:
    p = Path(settings.credential_dir)
    if not p.is_absolute():
        from .config import PROJECT_ROOT
        p = PROJECT_ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def _extract(content: str) -> tuple[dict[str, Any], str, str]:
    """解析 .info 内容 → (原始 dict, accessToken, uid)。"""
    text = (content or "").strip()
    if not text:
        raise ImportError_("内容是空的")
    try:
        data = json.loads(text)
    except Exception:
        raise ImportError_("不是合法 JSON —— 请粘贴 .info 文件的完整内容")
    if not isinstance(data, dict):
        raise ImportError_("内容结构不对：应该是包含 account / auth 的对象")
    raw_token = (data.get("auth") or {}).get("accessToken")
    token = raw_token if isinstance(raw_token, str) else ""
    uid = str((data.get("account") or {}).get("uid") or "")
    if not token or not uid:
        raise ImportError_("缺少 auth.accessToken 或 account.uid —— 这不是 WorkBuddy 的凭据文件")
    # 桌面端新版本把 token 存成 $wbEncrypted 信封，只有本机客户端运行时能解密。
    # 这种文件推上来也用不了，直接说清楚该换哪个文件，别让用户猜。
    if isinstance(raw_token, dict) or token.strip().startswith("$wbEncrypted"):
        raise ImportError_(
            "这是桌面端的加密凭据（token 加密在 $wbEncrypted 信封里，只有本机客户端能解开）。"
            "请改用：① CodeBuddy CLI 的凭据 Tencent-Cloud.coding-copilot.info，"
            "或 ② 签到脚本导出的明文 .info"
        )
    if len(token) < 100:
        raise ImportError_("accessToken 太短，看起来不完整（请确认文件没被截断）")
    return data, token, uid


def _existing_uid_map() -> dict[str, str]:
    """已有凭据文件里的 uid → label，用于「同一账号重复导入」时覆盖而不是新建。"""
    from .workbuddy_upstream import list_credential_files
    out: dict[str, str] = {}
    for label, path in list_credential_files().items():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            uid = str((data.get("account") or {}).get("uid") or "")
            if uid:
                out[uid] = label
        except Exception:
            continue
    return out


def _safe_label(raw: str) -> str:
    # 允许 * —— 账号标签可能是掩码手机号，不能把星号吃掉
    keep = [c for c in raw.strip() if c.isalnum() or c in "-_.@*"]
    label = "".join(keep)[:40].strip("._-")
    return label or f"account-{int(time.time())}"


def _verify(token: str, uid: str) -> dict[str, Any]:
    """用这份凭据真跑一次只读接口（独立函数便于测试替身）。"""
    from .workbuddy_upstream import fetch_billing
    return fetch_billing(token=token, uid=uid)


def import_credential(
    content: str, *, label: str | None = None, filename: str | None = None
) -> dict[str, Any]:
    """校验并落盘。返回账号摘要（不含 token）。"""
    data, token, uid = _extract(content)

    # 1) 先验证：余额接口是只读的，能通说明 token 真实有效
    try:
        billing = _verify(token, uid)
    except Exception as e:
        raise ImportError_(
            f"凭据校验失败（{type(e).__name__}）：可能已过期，请重新登录 WorkBuddy 后再导出"
        )

    # 2) 标签：显式传入 > 上传的文件名 > 昵称 > uid 后 4 位
    nickname = str((data.get("account") or {}).get("nickname") or "").strip()
    same = _existing_uid_map().get(uid)
    if same:
        final = same                       # 同一账号重复导入 → 覆盖原文件
    else:
        candidate = label or (Path(filename).stem if filename else "") or nickname or f"wb-{uid[-4:]}"
        final = _safe_label(candidate)

    # 3) 原子写 + 0600
    target = _dir() / f"{final}.info"
    tmp = target.with_suffix(".info.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)

    auth = data.get("auth") or {}

    def _sec(v):
        try:
            n = float(v)
        except (TypeError, ValueError):
            return None
        return n / 1000.0 if n > 1e11 else n

    return {
        "label": final,
        "nickname": nickname,
        "uid_tail": uid[-4:],
        "expires_at": _sec(auth.get("expiresAt")),
        "refresh_expires_at": _sec(auth.get("refreshExpiresAt")),
        "balance": billing.get("total"),
        "capacity": billing.get("capacity"),
        "used": billing.get("used"),
        "plan": billing.get("plan"),
        "file": target.name,
        "replaced": bool(same),
    }
