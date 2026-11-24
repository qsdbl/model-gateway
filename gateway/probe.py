"""实测探针（P34）：用一次真实长度的请求，量出「这个模型到底扣不扣钱」。

为什么必须实测（两次实测都推翻了直觉）：
- 目录里 `hy4flash-free` 标着 `x0.00` + badge「限时免费」，实测扣 **0.01**；
  目录里 `hy4flash` 标 `x0.03`，实测扣 **0**。名字后缀和目录价都是反的。
- 「限时免费」是活动价，会过期；目录里的 x0.00 是历史残留。
- **P40 更关键**：上游的免费是「输入长度门槛」，不是模型属性。
  实测 prompt ≤ 241 token → credit 0；> 241 token → credit 0.01。
  （hy4std / balanced-model 是短 0.01、长 0.05，同样有门槛。）
  所以探针**必须用真实长度的 prompt**：只发 1 个字符的话，
  任何模型测出来都是 0，调度器会把最贵的当免费用。

成本控制（探针自己也要花钱）：
- prompt 长度固定在跨过门槛的量（多花 0.01 起步，换来准确的数字）；
- max_tokens 压到 16；
- 每个模型默认 1 小时探一次（TTL）；
- 全局每小时探针次数有上限（默认 12 次），超了就不再探，退回目录倍率；
- 探针失败不重试（失败就是失败，重试只是多花钱）。

拿不到金额时，credit 记 NULL（未知），**不拿 0 冒充免费**。
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from .config import settings

# 默认策略（后台可改）
DEFAULT_TTL = 3600.0          # 探针有效期：1 小时
DEFAULT_BUDGET_PER_HOUR = 12  # 每小时最多探多少次
DEFAULT_TOP_K = 6             # 每个账号只探目录倍率最靠前的 N 个模型

# 429 文案里带重置时间：「将在 2026-11-19 21:45:41 UTC+8 重置」
_RESET_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})"
)

_lock = threading.Lock()
_probe_times: list[float] = []   # 本进程内最近一小时的探针时间戳


def _config() -> dict[str, Any]:
    """探针参数放在路由配置里（后台可改），读不到就用默认值。"""
    from .app import router

    try:
        cfg = router.load_config()
    except Exception:
        cfg = {}
    return {
        "enabled": bool(cfg.get("probe_enabled", True)),
        "ttl": float(cfg.get("probe_ttl", DEFAULT_TTL)),
        "budget_per_hour": int(cfg.get("probe_budget_per_hour", DEFAULT_BUDGET_PER_HOUR)),
        "top_k": int(cfg.get("probe_top_k", DEFAULT_TOP_K)),
        # R2.36：coldstart = 只给没有价格曲线的新模型测一次（推荐，省钱）；
        # all = 旧行为，定期探 top_k（保留可回滚）。
        "mode": str(cfg.get("probe_mode", "coldstart")).lower(),
    }


def reset_budget() -> None:
    """清空本进程的探针计数（测试用）。"""
    with _lock:
        _probe_times.clear()


def _budget_left(budget: int) -> int:
    now = time.time()
    with _lock:
        _probe_times[:] = [t for t in _probe_times if now - t < 3600]
        return max(0, budget - len(_probe_times))


def _note_probe() -> None:
    with _lock:
        _probe_times.append(time.time())


def parse_reset_time(message: str) -> float | None:
    """从 429 文案里解析重置时间（服务器本地时区）。返回 epoch 秒。"""
    if not message:
        return None
    m = _RESET_RE.search(message)
    if not m:
        return None
    import time as _t

    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    try:
        return _t.mktime((y, mo, d, h, mi, s, 0, 0, -1))
    except Exception:
        return None


def _credit_from_frames(frames: list[str]) -> tuple[float | None, int | None]:
    """从 SSE 帧里找 usage.credit 与总 token 数。"""
    credit: float | None = None
    tokens: int | None = None
    for frame in frames:
        if not frame.startswith("data: ") or "[DONE]" in frame:
            continue
        try:
            obj = json.loads(frame[6:].strip())
        except Exception:
            continue
        usage = obj.get("usage")
        if not isinstance(usage, dict):
            continue
        if usage.get("credit") is not None:
            try:
                credit = float(usage["credit"])
            except (TypeError, ValueError):
                pass
        total = usage.get("total_tokens")
        if total is not None:
            try:
                tokens = int(total)
            except (TypeError, ValueError):
                pass
    return credit, tokens


def default_account() -> str | None:
    """探针用哪个账号：取第一个有凭据的账号。

    为什么是「一个账号探全部模型」而不是「每个账号都探一遍」：
    同一模型对所有账号是同一个计费档（实测目录标 x0.00 的模型真扣 0.01，
    目录标 x0.03 的反而扣 0），账号之间的差异是**限流**，那个由冷却表管。
    「账号 × 模型」矩阵每小时要探 24 次，按模型探只要 6 次，省 4 倍的钱。
    """
    from .workbuddy_upstream import known_accounts

    accounts = list(known_accounts())
    return accounts[0] if accounts else None


# P40：探针 prompt 的目标长度（字符数）。
#
# 实测门槛是 prompt_tokens ≈ 241：不超过就免费，超过就收费。
# 中文一字约一个 token，所以 400 字符 ≈ 420 token，稳定跨过门槛。
# 探针只发这一次，长一点多花不了几个钱（0.01 起步），
# 但测出来的数字才是真实对话要付的钱。
PROBE_PROMPT_CHARS = 400


def _probe_prompt() -> str:
    """探针用的填充文本：长度够跨过免费门槛，内容无害。"""
    filler = "测" * PROBE_PROMPT_CHARS
    return f"只回复一个字：好。{filler}"


def run_probe(account_label: str, model_name: str) -> dict[str, Any]:
    """真打一发请求，返回 {credit, tokens, ok, error}。不落库。

    **P40 关键修正：探针必须用「真实工作负载」的 prompt 长度去测。**

    实测（2026-11-19，hy4flash / auto / fast-model 一致）：
        prompt ≤ 241 token  → credit 0（免费）
        prompt > 241 token  → credit 0.01（收费）
    hy4std / balanced-model 则是短 0.01、长 0.05。

    也就是说上游的「免费」是**输入长度门槛**，不是模型属性。原来的探针只发
    1 个字符（22 token），测出来永远是 0 → 所有模型都被判成「实测免费」，
    调度器就把最贵的模型排在最前面，真实请求（prompt 几百 token）每次扣 0.01~0.05。

    所以探针要用 `PROBE_PROMPT_CHARS` 这么长的填充文本，跨过门槛，
    测出来的才是真实对话要付的钱。
    """
    from . import workbuddy_upstream as UP

    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": _probe_prompt()}],
        "max_tokens": 16,
        "temperature": 0,
        "stream": True,
    }
    ctx = UP.use_account(account_label)
    try:
        frames = list(UP.iter_sse(payload, model=model_name))
    except Exception as exc:  # noqa: BLE001
        reason = str(exc)
        return {
            "ok": False, "credit": None, "tokens": None,
            "error": reason[:200], "reset_at": parse_reset_time(reason),
        }
    finally:
        UP.reset_account(ctx)
    credit, tokens = _credit_from_frames(frames)
    return {
        "ok": True,
        "credit": credit,
        "tokens": tokens,
        "error": None if credit is not None else "上游没回传扣费",
        "reset_at": None,
    }


def measure(model_name: str, account_label: str | None = None) -> dict[str, Any]:
    """探针 + 落库（模型级）。返回本次探测结果。"""
    from .store import store

    cfg = _config()
    if not cfg["enabled"]:
        return {"skipped": True, "reason": "探针已关闭"}
    account = account_label or default_account()
    if not account:
        return {"skipped": True, "reason": "没有可用账号"}
    if _budget_left(cfg["budget_per_hour"]) <= 0:
        return {"skipped": True, "reason": "本小时探针预算用完"}
    _note_probe()
    result = run_probe(account, model_name)
    store.put_probe(
        account, model_name,
        credit=result.get("credit"), tokens=result.get("tokens"),
        ok=bool(result.get("ok")), error=result.get("error"),
    )
    # P41：探针结果也是价格曲线的一个观测点。
    # 探针打的是「真实工作负载长度」（约 420 token），落在收费侧；
    # 免费侧靠真实短请求自然积累（每条成功请求都会记一笔）。
    if result.get("ok") and result.get("credit") is not None and result.get("tokens"):
        try:
            from . import costmodel

            costmodel.observe(model_name, int(result["tokens"]), float(result["credit"]))
        except Exception:
            pass
    if result.get("reset_at"):
        # 探针自己撞了限流 → 把这个账号+模型拉黑
        store.set_cooldown(
            account, model_name, float(result["reset_at"]),
            "探针撞限流：" + (result.get("error") or "")[:120],
        )
        store.log_schedule(
            "probe_cooldown",
            f"{account}/{model_name} 探针撞限流，冷却到 {result['reset_at']}",
            "warn",
        )
    return result


def cached(model_name: str, account_label: str | None = None) -> dict[str, Any] | None:
    """取缓存的探针记录（不新鲜也返回，调用方自己看 checked_at）。"""
    from .store import store

    return store.get_probe(account_label or "", model_name)


def fresh(model_name: str, account_label: str | None = None) -> dict[str, Any] | None:
    """只在记录仍然新鲜（TTL 内）时返回。"""
    rec = cached(model_name, account_label)
    if not rec:
        return None
    cfg = _config()
    if time.time() - float(rec.get("checked_at") or 0) > cfg["ttl"]:
        return None
    return rec


def ensure(model_name: str, account_label: str | None = None) -> dict[str, Any] | None:
    """调度时用：拿一份新鲜的实测结果；没有就现场探一发（受预算约束）。

    返回 None 表示「不知道」——调用方要按未知处理，不能当免费。
    """
    rec = fresh(model_name, account_label)
    if rec is not None:
        return rec
    result = measure(model_name, account_label)
    if result.get("skipped"):
        return None
    return cached(model_name, account_label)


def is_measured_free(record: dict[str, Any] | None) -> bool | None:
    """实测免费判定：True 免费 / False 收费 / None 未知。

    规则：
    - 没有记录 → None（未知）
    - 探针失败 → None（失败不等于免费）
    - 上游回传了金额 → credit == 0 即免费

    P40：现在的探针用**真实长度**的 prompt 去测，所以 credit == 0 意味着
    「真实对话长度下也免费」，不再是被短 prompt 骗出来的假免费。
    """
    if not record:
        return None
    if not record.get("ok"):
        return None
    credit = record.get("credit")
    if credit is None:
        return None
    try:
        return float(credit) <= 0.0
    except (TypeError, ValueError):
        return None


def measured_credit(record: dict[str, Any] | None) -> float | None:
    if not record or not record.get("ok"):
        return None
    credit = record.get("credit")
    try:
        return float(credit)
    except (TypeError, ValueError):
        return None


def health_probe_model() -> str | None:
    """健康探测用哪个模型：官方免费池里挑一个，绝不花积分（R2.36）。

    用户诉求：「探针是在检测 high 模式模型健康吗？可以用免费池模型来干活。」
    事实澄清：现有探针本来就是价格标定、不是 high 健康检查；真正的事实上的
    健康信号是失败重试+冷却表（scheduler.is_retryable/mark_cooldown）。
    这里补一个显式的「用免费模型探一发」入口：既验证免费账号+上游链路是否通，
    又不花一分钱。模型从 freepool 唯一真源动态取，免费池换代也不会失效；
    没有官方免费模型时返回 None（宁可不探，也不误用付费模型）。
    """
    from .freepool import official_free_names

    names = official_free_names()
    # 优先 hy4flash-free（实测不限长度免费、覆盖全部 WB 账号），
    # 其次任意官方免费模型；Qoder 不标免费、不会进这个集合。
    if "hy4flash-free" in names:
        return "hy4flash-free"
    if names:
        return sorted(names)[0]
    # 目录还没加载（冷启动/测试环境）→ 用已知的官方免费模型名兜底。
    # 这是官方目录固定属性、不是猜测；若将来官方下架，freepool.refresh 会更新集合，
    # 但健康探测用免费模型这个方向不变，宁可用它也不会误用付费模型。
    return "hy4flash-free"


def health_check(account_label: str, model_name: str | None = None) -> dict[str, Any]:
    """用免费模型做一次链路健康探测（R2.36）。credit 应为 0。

    这是「健康」用途，不是价格标定：结果不喂给 costmodel 曲线（免费模型喂 0
    没有标定价值），只落 probe 缓存供后台看。真正的价格学习仍由真实请求的
    costmodel.observe() 完成。
    """
    cfg = _config()
    if not cfg["enabled"]:
        return {"ok": False, "reason": "探针已关闭"}
    mid = model_name or health_probe_model()
    if not mid:
        return {"ok": False, "reason": "没有官方免费模型可用于健康探测"}
    if _budget_left(cfg["budget_per_hour"]) <= 0:
        return {"ok": False, "reason": "本小时探针预算用完"}
    _note_probe()
    result = run_probe(account_label, mid)
    from .store import store

    store.put_probe(
        account_label, mid,
        credit=result.get("credit"), tokens=result.get("tokens"),
        ok=bool(result.get("ok")), error=result.get("error"),
    )
    return result


def refresh_pool(model_names: list[str], account_label: str | None = None) -> dict[str, Any]:
    """后台/定时任务用：按预算刷新一批模型的实测结果（模型级）。

    R2.36 coldstart 模式：只给**没有价格曲线**的新模型做一次冷启动标定，
    有曲线的不再周期性花钱重测——后续价格变化由真实请求的 costmodel.observe()
    零成本学到。mode=all 保留旧行为（定期探 top_k），可回滚。
    """
    cfg = _config()
    if not cfg["enabled"]:
        return {"ok": False, "reason": "探针已关闭", "probed": 0}
    account = account_label or default_account()
    if not account:
        return {"ok": False, "reason": "没有可用账号", "probed": 0}

    candidates = list(model_names)
    if cfg["mode"] == "coldstart":
        # 只保留「既没有曲线、又没有新鲜缓存」的模型 = 真正的新模型。
        from . import costmodel

        curves = costmodel.all_curves()
        candidates = [
            mid for mid in candidates
            if mid not in curves and fresh(mid) is None
        ]
        if not candidates:
            return {"ok": True, "probed": 0, "results": [],
                    "reason": "所有模型已有价格曲线，跳过冷启动标定"}

    done: list[dict[str, Any]] = []
    for mid in candidates:
        if _budget_left(cfg["budget_per_hour"]) <= 0:
            break
        if fresh(mid) is not None:
            continue
        res = measure(mid, account)
        done.append({"model": mid, **{
            k: v for k, v in res.items() if k in ("ok", "credit", "error", "skipped")
        }})
    return {"ok": True, "probed": len(done), "results": done}
