"""运行时可切换的上游模型状态（P15）。

为什么需要：账号被限流（429）时，免费模型不可用，必须能**手动切到另一个模型**，
而不是改 .env 再重启服务。这里用一个很小的 JSON 状态文件承载「当前模型」，
网关读它决定发给上游的 model 字段。

设计约束：
- 只存模型 id 与时间戳，不存任何凭据。
- 原子写（临时文件 + os.replace），进程崩溃不会留下半截 JSON。
- 文件不存在 / 内容损坏 → 视为未切换，回退到环境变量 GATEWAY_WB_MODEL。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .config import settings

_LOCK = threading.Lock()
_FILENAME = "state.json"


def _path() -> Path:
    return Path(settings.data_dir) / _FILENAME


def _read() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def active_model() -> str | None:
    """当前手动指定的模型 id；None 表示用环境变量默认值。"""
    with _LOCK:
        m = _read().get("active_model")
    return m if isinstance(m, str) and m else None


def active_account() -> str | None:
    """当前手动指定的账号标签（P19）；None 表示按余额自动挑。"""
    with _LOCK:
        a = _read().get("active_account")
    return a if isinstance(a, str) and a else None


def state_snapshot() -> dict[str, Any]:
    with _LOCK:
        d = _read()
    return {
        "active_model": d.get("active_model"),
        "active_account": d.get("active_account"),
        "updated_at": d.get("updated_at"),
        "updated_by": d.get("updated_by"),
    }


def set_active_model(model: str, *, by: str = "admin") -> dict[str, Any]:
    payload = _patch({"active_model": model, "updated_at": time.time(), "updated_by": by})
    return payload


def clear_active_model(*, by: str = "admin") -> dict[str, Any]:
    payload = _patch({"active_model": None, "updated_at": time.time(), "updated_by": by})
    return payload


def set_active_account(label: str | None, *, by: str = "admin") -> dict[str, Any]:
    return _patch({"active_account": label, "updated_at": time.time(), "updated_by": by})


def _patch(changes: dict[str, Any]) -> dict[str, Any]:
    """合并写入：改模型不能顺手把账号选择抹掉，反之亦然。"""
    with _LOCK:
        payload = _read()
    payload.update(changes)
    _write(payload)
    return payload


def _write(payload: dict[str, Any]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
