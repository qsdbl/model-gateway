"""价格标定任务（P41）：后台跑，前端轮询进度。

为什么要异步：
一次标定 = N 个模型 × 5 档长度 = 5N 次真实请求，**实测每次约 7 秒**
（2 个模型 10 次请求用了 70 秒）。同步接口会让浏览器等好几分钟，
用户以为卡死了。所以改成：POST 立刻返回、后台线程跑、GET 查进度。

进度状态是**进程内**的（重启就丢），这没关系：标定是手动触发的临时任务，
真丢了用户再点一次就行，不需要持久化。
"""
from __future__ import annotations

import threading
import time
from typing import Any

# 每档长度（字符数）。对应约 100 / 400 / 1200 / 2500 / 5000 token。
LENGTHS = (100, 400, 1200, 2500, 5000)

_lock = threading.Lock()
_state: dict[str, Any] = {
    "running": False,
    "done": 0,
    "total": 0,
    "current": "",
    "started_at": 0.0,
    "finished_at": 0.0,
    "results": [],
    "error": "",
}


def snapshot() -> dict[str, Any]:
    """当前标定进度。"""
    with _lock:
        return dict(_state)


def start(models: list[str], account_label: str | None = None) -> dict[str, Any]:
    """启动一次标定。已在跑就返回当前状态（不重复启动）。"""
    with _lock:
        if _state.get("running"):
            return {"started": False, "reason": "已有标定在跑", **dict(_state)}
        _state.update({
            "running": True, "done": 0, "total": len(models), "current": "",
            "started_at": time.time(), "finished_at": 0.0, "results": [], "error": "",
        })

    t = threading.Thread(
        target=_run, args=(list(models), account_label), daemon=True,
        name="pricing-calibrate",
    )
    t.start()
    return {"started": True, **snapshot()}


def _set(**kw: Any) -> None:
    with _lock:
        _state.update(kw)


def _run(models: list[str], account_label: str | None) -> None:
    from . import costmodel
    from . import workbuddy_upstream as UP

    try:
        accounts = list(UP.known_accounts())
        if not accounts:
            _set(error="没有可用账号", running=False, finished_at=time.time())
            return
        account = account_label or accounts[0]
        results: list[dict[str, Any]] = []
        for mid in models:
            _set(current=mid)
            rec: dict[str, Any] = {"model": mid, "points": []}
            for chars in LENGTHS:
                credit, tokens, err = _one_shot(mid, chars, account)
                if err:
                    rec["points"].append({"chars": chars, "error": err})
                else:
                    rec["points"].append(
                        {"chars": chars, "credit": credit, "tokens": tokens}
                    )
                    if credit is not None and tokens:
                        costmodel.observe(mid, int(tokens), float(credit))
            results.append(rec)
            _set(results=list(results), done=len(results))
        costmodel.reload()
        _set(running=False, current="", finished_at=time.time())
    except Exception as exc:  # noqa: BLE001
        _set(running=False, error=str(exc)[:200], finished_at=time.time())


def _one_shot(model: str, chars: int, account: str) -> tuple[float | None, int | None, str | None]:
    """打一发，返回 (credit, prompt_tokens, error)。"""
    import json as _json

    from .credit import credit_from_usage, prompt_tokens_from_usage
    from .workbuddy_upstream import iter_sse, reset_account, use_account

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "好。" + "测" * chars}],
        "max_tokens": 16, "temperature": 0, "stream": True,
    }
    ctx = use_account(account)
    try:
        frames = list(iter_sse(payload, model=model))
    except Exception as exc:  # noqa: BLE001
        return None, None, str(exc)[:120]
    finally:
        reset_account(ctx)

    credit: float | None = None
    tokens: int | None = None
    for f in frames:
        if not f.startswith("data: ") or "[DONE]" in f:
            continue
        try:
            obj = _json.loads(f[6:].strip())
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(obj, dict):
            continue
        usage = obj.get("usage")
        if not isinstance(usage, dict):
            continue
        if credit is None:
            credit = credit_from_usage(usage)
        if tokens is None:
            tokens = prompt_tokens_from_usage(usage)
    return credit, tokens, None

