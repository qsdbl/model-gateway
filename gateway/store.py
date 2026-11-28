"""SQLite 持久化：账号、模型目录缓存、请求记录。

原则（README 第 5 节）：
- 凭据不进 DB（只存凭据文件路径与账号元信息）。
- Token 估算、目录倍率、实际扣费分开字段；取不到真实值时存 NULL(显示未知)。
- 数据库文件跨重启保留，绝不删除。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import settings


_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT NOT NULL UNIQUE,       -- 显示标签(手机号掩码等)
    platform        TEXT NOT NULL,              -- workbuddy | qoder
    credential_path TEXT NOT NULL,              -- 0600 凭据文件路径，内容不进 DB
    auth_status     TEXT NOT NULL DEFAULT 'unknown',  -- ok | expired | unknown | disabled
    balance         REAL,                       -- 可用余额（平台口径，见 protocol-notes）
    balance_unit    TEXT,                       -- credits_wb / credits_qoder（不互比）
    balance_checked_at REAL,
    enabled         INTEGER NOT NULL DEFAULT 1, -- 是否参与路由
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS models (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    model_name      TEXT NOT NULL,
    public_name     TEXT,                       -- 发布名(可能为 NULL)
    is_free         INTEGER,                    -- 1/0/NULL(未知)
    price_factor    REAL,                       -- 倍率
    promotion_until REAL,                       -- 优惠截止 epoch
    tools           INTEGER,
    image           INTEGER,
    context_window  INTEGER,
    raw             TEXT,                       -- 原始目录 JSON 片段
    fetched_at      REAL NOT NULL,
    UNIQUE(platform, model_name)
);

CREATE TABLE IF NOT EXISTS requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      REAL NOT NULL,
    public_model    TEXT NOT NULL,
    platform        TEXT,                       -- 实际平台
    model_name      TEXT,                       -- 实际模型
    account_label   TEXT,                       -- 实际账号
    choose_reason   TEXT,                       -- 选择原因
    stream          INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,              -- success | error | cancelled
    error           TEXT,
    latency_ms      INTEGER,
    token_estimate  INTEGER,                    -- Token 估算（非账单）
    price_factor    REAL,                       -- 目录倍率（非扣费）
    actual_credit   REAL,                       -- 可取得的真实扣费；NULL=未知
    payload         TEXT                        -- 请求摘要(脱敏)，不含凭据
);

CREATE INDEX IF NOT EXISTS idx_requests_created ON requests(created_at);
CREATE INDEX IF NOT EXISTS idx_models_platform ON models(platform);

-- P17：后台登录会话。sid 是随机 32 字节十六进制，cookie 里再拼 HMAC 签名。
-- 存库的目的不是鉴权（签名已经够），而是「可撤销 + 可审计 + 滑动续期」。
CREATE TABLE IF NOT EXISTS admin_sessions (
    sid         TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    expires_at  REAL NOT NULL,              -- 空闲到期时间（每次访问滑动续期）
    ip          TEXT,
    ua          TEXT,
    revoked     INTEGER NOT NULL DEFAULT 0
);

-- P17：登录审计。只记「结果 + 原因」，绝不记令牌本身或任何前缀。
CREATE TABLE IF NOT EXISTS admin_auth_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL NOT NULL,
    ip          TEXT,
    ua          TEXT,
    path        TEXT,
    result      TEXT NOT NULL,              -- ok | deny | unban
    reason      TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_events_created ON admin_auth_events(created_at);
CREATE INDEX IF NOT EXISTS idx_auth_events_ip ON admin_auth_events(ip, created_at);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_exp ON admin_sessions(expires_at);

-- 通用键值配置：找回邮箱、SMTP 配置这类后台可改的小设置。
-- SMTP 授权码也存这里（后台「安全」页配置，页面只显示「是否已设置」不回显明文）；
-- 环境变量 GATEWAY_SMTP_* 作为兜底，两者都不配则发信不可用。
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  REAL NOT NULL
);

-- P20：邮件一次性登录链接。库里只存 token 的 sha256，泄库也换不出可用链接。
CREATE TABLE IF NOT EXISTS admin_magic (
    id          TEXT PRIMARY KEY,           -- sha256(token) 十六进制
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    used_at     REAL,
    ip          TEXT,
    ua          TEXT
);

CREATE INDEX IF NOT EXISTS idx_magic_exp ON admin_magic(expires_at);

-- P27：账单快照。平台账单只给「当前已用总量」，不给按小时明细，所以每次后台
-- 刷新账号页时把 used 存一条；两条快照的差值就是这段时间真实消耗（含官方客户端）。
-- 为什么不用 requests.actual_credit 当「今日消耗」：它只覆盖经过网关的请求，
-- 不含官方客户端与网页版，天生对不上账单（P29）。另外 P30 之前流式路径压根没记账，
-- 那一列长期 88% 是 NULL——现在流式也接了（P30），但历史行仍是 NULL，不能回溯。
CREATE TABLE IF NOT EXISTS usage_snapshots (
    account_label   TEXT NOT NULL,
    ts              REAL NOT NULL,
    used            REAL NOT NULL,
    PRIMARY KEY (account_label, ts)
);

CREATE INDEX IF NOT EXISTS idx_snap_label_ts ON usage_snapshots(account_label, ts);

-- R2.20：额度快照。usage_snapshots 只记 used（账单已用），看不出「现在还有多少
-- 积分 / 是不是新号 0 额度 / 活动停发 / 每天还能领多少」。每小时用各账号 token 读
-- 官方只读接口存一条，变化（0→有、有→0、userType 变化、活动上/下线）即触发提醒，
-- 也能画积分曲线。
-- 只读不 claim——签到仍归 daily_report 每天一次，别把签到变成每小时。
-- claim_active：每日签到活动当前是否可领（1 可领100 / 0 已结束领0 / NULL 查不到）。
CREATE TABLE IF NOT EXISTS quota_snapshots (
    account_label   TEXT NOT NULL,
    ts              REAL NOT NULL,
    platform        TEXT NOT NULL,
    available       REAL,            -- 当前可用积分；NULL=查不到（不估算）
    quota_total     REAL,            -- 总额度（含 addon）
    user_type       TEXT,            -- personal_standard / personal_professional_trial
    exceeded        INTEGER,         -- isQuotaExceeded
    claim_active    INTEGER,         -- 每日签到活动是否可领
    PRIMARY KEY (account_label, ts)
);
CREATE INDEX IF NOT EXISTS idx_quota_label_ts ON quota_snapshots(account_label, ts);

-- P34：实测探针结果。「免费」不能靠名字后缀猜（实测目录标 x0.00 的模型真扣 0.01，
-- 目录标 x0.03 的反而扣 0），只能自己打一发极短请求读上游回传的 usage.credit。
-- P34/P40：实测探针结果，**模型级**（不是账号级）。
-- 同一模型对所有账号是同一个计费档（实测：目录标 x0.00 的 hy4flash-free 真扣 0.01，
-- 目录标 x0.03 的 hy4flash 反而扣 0），所以按模型存一份就够，
-- 「账号 × 模型」矩阵是浪费——每小时探 6 个模型比探 24 次便宜 4 倍。
-- 账号级的差异是「限流」，由 cooldowns 表管。
CREATE TABLE IF NOT EXISTS model_probes (
    model_name   TEXT PRIMARY KEY,
    account_label TEXT,           -- 用哪个账号探的（只是记录，不参与判定）
    credit       REAL,            -- 上游回传的实际扣费；NULL=没拿到（未知，不当免费）
    tokens       INTEGER,
    checked_at   REAL NOT NULL,
    ok           INTEGER NOT NULL DEFAULT 1,
    error        TEXT
);

-- P41：价格曲线观测点。上游是「按请求输入 token 计费」，免费只是一段长度区间
-- （实测 hy4flash ≤123 token 免费，423 token 起 0.01 且随长度线性涨到 0.08）。
-- 所以「模型是免费模型 / 收费模型」这个二分是错的维度——必须记 (token, credit)
-- 的观测点，才能算出「这次这个长度的请求，用哪个模型最便宜」。
--
-- points 存 JSON 数组 [[tokens, credit], ...]，按 tokens 升序。
-- 数据来源：真实请求的 usage（零额外成本，主力）+ 探针（冷启动/校正）。
CREATE TABLE IF NOT EXISTS model_pricing (
    model_name TEXT PRIMARY KEY,
    points     TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_probes_checked ON model_probes(checked_at);

-- P35：冷却表。上游 429 会带重置时间，把它拉黑到那一刻，到期自动放回。
-- 落库而不是放内存：网关重启后不该再踩同一个已限流的号。
CREATE TABLE IF NOT EXISTS cooldowns (
    account_label TEXT NOT NULL,
    model_name    TEXT NOT NULL,
    until_at      REAL NOT NULL,
    reason        TEXT,
    created_at    REAL NOT NULL,
    PRIMARY KEY (account_label, model_name)
);

CREATE INDEX IF NOT EXISTS idx_cooldown_until ON cooldowns(until_at);

-- P35：轮转计数。同一模型下按「用得少的账号优先」把额度摊平到各账号，
-- 而不是像旧逻辑那样永远先用余额最多的那个。
CREATE TABLE IF NOT EXISTS rotation (
    account_label TEXT NOT NULL,
    model_name    TEXT NOT NULL,
    n             INTEGER NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL,
    PRIMARY KEY (account_label, model_name)
);

-- R2.35：带时间戳的轮转事件。旧的 rotation 表存历史累计（永久只增），
-- 一旦拉开差距就会长期偏向少数账号（10 个 WB 账号只有 5 个被轮询到）。
-- 事件表让调度按「最近 N 分钟用了几次」排，窗口滑过就自然归零、公平轮转。
-- 累计表仍保留供追溯，二者由 bump_rotation 同步写。
CREATE TABLE IF NOT EXISTS rotation_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_label TEXT NOT NULL,
    model_name    TEXT NOT NULL,
    used_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rotation_events_lookup
    ON rotation_events(model_name, used_at);

-- R2.33：凭据续期失败状态。连续失败 N 次发一封邮件提醒用户去重新扫码，
-- 避免凭据到期掉线而无感知。成功即清零；提醒后记 notified_at 防止重复轰炸。
CREATE TABLE IF NOT EXISTS renew_state (
    account_label TEXT NOT NULL,
    platform      TEXT NOT NULL,
    fail_streak   INTEGER NOT NULL DEFAULT 0,
    last_fail_at  REAL,
    notified_at   REAL,
    PRIMARY KEY (account_label, platform)
);

-- P37：调度日志。「429 → 冷却 → 换号 → 成功」这条链路要一眼看懂。
CREATE TABLE IF NOT EXISTS schedule_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    level      TEXT NOT NULL,      -- info | warn | error
    event      TEXT NOT NULL,
    detail     TEXT
);

CREATE INDEX IF NOT EXISTS idx_schedlog_created ON schedule_log(created_at);
"""


def _rotation_window_seconds() -> float:
    """轮转公平窗口（秒）。配置在 routes.json 的 rotation_window_minutes。

    默认 30 分钟：足够让一轮请求摊平到所有账号，又不会像历史累计那样
    一次拉开差距就永久固化。配 0/负数退回旧的历史累计口径（可回滚）。
    """
    # 直接读配置文件，避免 from .router import router（router.py 只定义类，
    # 实例在 app.py；从 store 反向导 app 会循环依赖）。路径与 Router 默认一致。
    try:
        import json as _json

        path = settings.data_dir / "routes.json"
        raw = _json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        minutes = float(raw.get("rotation_window_minutes", 30))
    except Exception:
        minutes = 30.0
    return max(0.0, minutes) * 60.0


class Store:
    """线程安全的 SQLite 封装。用 check_same_thread=False + 锁，适配 FastAPI 线程池。"""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else settings.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    # accounts 表在 P33 增加了列。SQLite 的 CREATE TABLE IF NOT EXISTS 不会给
    # 已存在的表补列，所以这里显式补齐（老库直接 ALTER，不动已有数据）。
    _ACCOUNT_COLUMNS = {
        "source": "TEXT",        # 来源：签到系统 / 后台导入
        "expires_at": "REAL",    # 凭据到期
        "nickname": "TEXT",
        "last_sync_at": "REAL",  # 最近一次同步
        "quota": "REAL",         # 平台额度百分比（Qoder 口径）
        "quota_checked_at": "REAL",
        "note": "TEXT",
    }
    # R2.33：额度监控表列迁移表驱动（与 _SCHEMA 的 quota_snapshots 定义保持一致）。
    # 旧库缺哪列补哪列；将来加新列只改这里 + _SCHEMA，不用再写一次性 ALTER。
    _QUOTA_COLUMNS = {
        # ALTER 加列必须可空/有默认值，否则旧库已有行会失败——故这里不带 NOT NULL。
        "platform": "TEXT",
        "available": "REAL",
        "quota_total": "REAL",
        "user_type": "TEXT",
        "exceeded": "INTEGER",
        "claim_active": "INTEGER",
    }

    def _migrate(self) -> None:
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(accounts)")}
        for name, typ in self._ACCOUNT_COLUMNS.items():
            if name not in cols:
                self._conn.execute(f"ALTER TABLE accounts ADD COLUMN {name} {typ}")
        # P40：model_probes 从「账号 × 模型」改成「模型级」主键。
        # 这张表只存缓存的测量值（不是账单、不是审计），直接重建即可。
        #
        # 判据必须是**主键**，不能是「有没有 account_label 列」——
        # 新表也有 account_label 列（只作备注），用列名判断会导致每次重启
        # 都把探针数据 DROP 掉，线上踩过：重启后实测数据全没，调度器
        # 退回目录价（而目录价是错的），免费模型因此被当成收费。
        probe_cols = [r[1] for r in self._conn.execute("PRAGMA table_info(model_probes)")]
        probe_pk = [r[1] for r in self._conn.execute(
            "PRAGMA table_info(model_probes)") if r[5]]      # r[5]=pk 序号
        if probe_cols and probe_pk != ["model_name"]:
            self._conn.execute("DROP TABLE model_probes")
            self._conn.execute(
                """CREATE TABLE model_probes (
                       model_name   TEXT PRIMARY KEY,
                       account_label TEXT,
                       credit       REAL,
                       tokens       INTEGER,
                       checked_at   REAL NOT NULL,
                       ok           INTEGER NOT NULL DEFAULT 1,
                       error        TEXT
                   )"""
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_probes_checked ON model_probes(checked_at)"
            )
        # P41：requests 加 prompt_tokens 列（上游 usage 给的**真实**输入 token）。
        # 这是价格曲线的主要数据源——每条真实请求都产出一组 (model, tokens, credit)，
        # 零额外成本就能把模型价格学出来。token_estimate 是估算值，两者不能混用。
        req_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(requests)")}
        if "prompt_tokens" not in req_cols:
            self._conn.execute("ALTER TABLE requests ADD COLUMN prompt_tokens INTEGER")
        # R2.20/R2.33：额度监控表列迁移。旧库的 quota_snapshots 缺列会报
        # "table quota_snapshots has no column named claim_active"（08:20 实测踩到）。
        # 原写法 `if quota_cols and ...` 依赖表已存在；改成按 columns 定义表驱动、
        # 逐列检测幂等补齐——将来再加新列只要往这个 dict 里加一行，不必再改迁移代码。
        # 表不存在时 _SCHEMA 已建好（带全部列），这里自然查不到缺列、不会误 ALTER。
        quota_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(quota_snapshots)")}
        for name, typ in self._QUOTA_COLUMNS.items():
            if quota_cols and name not in quota_cols:
                self._conn.execute(f"ALTER TABLE quota_snapshots ADD COLUMN {name} {typ}")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ---------- 请求记录 ----------
    def record_request(self, *, public_model: str, stream: bool, payload: dict[str, Any]) -> int:
        summary = {
            "messages": len(payload.get("messages") or []),
            "tools": len(payload.get("tools") or []),
            "has_image": any(
                isinstance(m.get("content"), list)
                and any(
                    isinstance(p, dict) and p.get("type") == "image_url"
                    for p in m["content"]
                )
                for m in (payload.get("messages") or [])
                if isinstance(m, dict)
            ),
        }
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO requests
                   (created_at, public_model, stream, status, payload)
                   VALUES (?, ?, ?, ?, ?)""",
                (time.time(), public_model, int(stream), "pending", json.dumps(summary)),
            )
            return int(cur.lastrowid)

    def attach_route(
        self,
        req_id: int,
        *,
        platform: str | None,
        model_name: str | None,
        account_label: str | None,
        choose_reason: str | None = None,
    ) -> None:
        """路由确定后立刻回填本次请求的实际平台/模型/账号（R2.22）。

        record_request() 在路由决策之前落库，pending 期间 model_name 为空 → 后台
        明细里显示 unknown。调度一旦选出 chosen 就调本方法写回，这样「进行中」的
        行也有真实模型名，不必等请求结束 finalize。幂等：只写路由字段，不碰 status。
        """
        with self._tx() as c:
            c.execute(
                """UPDATE requests SET platform=?, model_name=?, account_label=?,
                     choose_reason=? WHERE id=?""",
                (platform, model_name, account_label, choose_reason, req_id),
            )

    def finalize_request(
        self,
        req_id: int,
        *,
        status: str,
        platform: str | None = None,
        model_name: str | None = None,
        account_label: str | None = None,
        choose_reason: str | None = None,
        error: str | None = None,
        latency_ms: int | None = None,
        token_estimate: int | None = None,
        price_factor: float | None = None,
        actual_credit: float | None = None,
        prompt_tokens: int | None = None,
    ) -> None:
        with self._tx() as c:
            # R2.22：路由字段（platform/model_name/account_label/choose_reason）用
            # COALESCE(新值, 旧值)——attach_route 已回填的真实模型名不能被 finalize
            # 的默认 None 抹掉（错误路径 finalize 不传这些字段，实测会覆盖成 NULL）。
            c.execute(
                """UPDATE requests SET
                     status=?, platform=COALESCE(?,platform), model_name=COALESCE(?,model_name),
                     account_label=COALESCE(?,account_label),
                     choose_reason=COALESCE(?,choose_reason),
                     error=?, latency_ms=?, token_estimate=?,
                     price_factor=?, actual_credit=?, prompt_tokens=?
                   WHERE id=?""",
                (
                    status, platform, model_name, account_label, choose_reason,
                    error, latency_ms, token_estimate, price_factor, actual_credit,
                    prompt_tokens, req_id,
                ),
            )

    def list_requests(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_schedule(self, window_minutes: int = 5,
                        public_model: str | None = None) -> dict[str, Any]:
        """R2.39b：最近 N 分钟的**真实调度**聚合（模型 tab 用量口径对齐）。

        用户诉求：模型 tab 以前是「典型短请求预览」，用量汇总是历史真实请求，
        两边对不上还各说各话。这里直接读 requests 表的真实记录，返回：
        - window_minutes：统计窗口
        - total：窗口内请求总数
        - items：按 (platform, model_name, account_label) 聚合，各带次数和最近一次时间
        - segments：按时间正序的切换段（连续相同 账号×模型 折叠），展示「在谁之间切换」
        """
        cutoff = time.time() - max(1, window_minutes) * 60
        # R2.51：按对外模型（low/high）过滤——模型页两个 tab 各看自己档位的真实调度。
        where_pm = " AND public_model = ?" if public_model else ""
        args = (cutoff, public_model) if public_model else (cutoff,)
        with self._lock:
            rows = self._conn.execute(
                """SELECT platform, model_name, account_label, created_at, status,
                          price_factor
                   FROM requests
                   WHERE account_label IS NOT NULL AND model_name IS NOT NULL
                     AND created_at >= ?""" + where_pm + """
                   ORDER BY id ASC""",
                args,
            ).fetchall()
        rows = [dict(r) for r in rows]
        from collections import Counter
        agg = Counter((r["platform"] or "?", r["model_name"],
                       r["account_label"]) for r in rows)
        items = [
            {
                "platform": p, "model_name": m, "account_label": a,
                "count": n,
                # 目录倍率快照（请求落库时记录的）；多次取最新一条非空值
                "price_factor": next(
                    (r["price_factor"] for r in reversed(rows)
                     if (r["platform"] or "?") == p and r["model_name"] == m
                     and r["account_label"] == a
                     and r["price_factor"] is not None), None),
                "last_at": max(r["created_at"] for r in rows
                               if (r["platform"] or "?") == p
                               and r["model_name"] == m
                               and r["account_label"] == a),
            }
            for (p, m, a), n in agg.items()
        ]
        items.sort(key=lambda x: (-x["count"], -x["last_at"]))
        # 切换段：连续相同 (账号, 模型) 折叠
        segments: list[dict[str, Any]] = []
        for r in rows:
            key = (r["account_label"], r["model_name"], r["platform"])
            if segments and segments[-1]["key"] == key:
                segments[-1]["count"] += 1
                segments[-1]["until"] = r["created_at"]
            else:
                segments.append({
                    "key": key, "account_label": r["account_label"],
                    "model_name": r["model_name"], "platform": r["platform"],
                    "count": 1, "since": r["created_at"], "until": r["created_at"],
                })
        for s in segments:
            s.pop("key", None)
        return {
            "window_minutes": window_minutes,
            "total": len(rows),
            "items": items,
            "segments": segments[-12:],   # 最多回看 12 段，防止刷屏
        }

    def switch_history(self, limit: int = 300, days: int | None = None) -> list[dict[str, Any]]:
        """把最近的请求按「连续相同 (账号, 模型)」折叠成切换段。

        用户反馈：「今天切到 153 账号的免费 Hy4 flash，是因为昨天 176 限额吗？
        不然每次得猜」。requests.choose_reason 里其实已经有原因，但一条条看
        请求记录看不出「什么时候从谁切到谁、持续了多久」。这里按时间正序读，
        相邻两条 (account_label, model_name) 不同就切一段，返回最近的段（倒序，
        最新的在前）。失败请求也保留在同一个段里——段内 errors 计数能说明
        「这一段是不是撞了限流」。

        R2.26：days 给定时按**时间窗口**取（最近 N 天全部请求），不再按条数 LIMIT。
        原因：只取最近 300 条请求时，一旦近期 low 请求密集，就会把几小时前的
        high 调用挤出窗口，最近使用列表显示 high 0（明明数据里有 high）。
        一周全量才 5000 多条，完全拉得起；前端「最近使用」据此聚合，
        桑基图/时间轴仍只画最近 300 段（在前端切片）。
        """
        with self._lock:
            if days is not None:
                cutoff = time.time() - days * 86400
                rows = self._conn.execute(
                    """SELECT id, created_at, account_label, model_name, platform,
                              choose_reason, status, actual_credit, token_estimate,
                              CASE WHEN public_model='high' THEN 'high' ELSE 'low' END AS public_mode
                       FROM requests
                       WHERE account_label IS NOT NULL AND created_at >= ?
                       ORDER BY id DESC""",
                    (cutoff,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """SELECT id, created_at, account_label, model_name, platform,
                              choose_reason, status, actual_credit, token_estimate,
                              CASE WHEN public_model='high' THEN 'high' ELSE 'low' END AS public_mode
                       FROM requests
                       WHERE account_label IS NOT NULL
                       ORDER BY id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
        # 上面是倒序；转成正序才好判断「上一段→这一段」
        rows = list(reversed([dict(r) for r in rows]))
        segments: list[dict[str, Any]] = []
        for r in rows:
            key = (r["account_label"], r["model_name"])
            if segments and segments[-1]["_key"] == key:
                seg = segments[-1]
                seg["requests"] += 1
                seg["last_at"] = r["created_at"]
                seg["tokens"] += int(r["token_estimate"] or 0)
                if r["status"] == "error":
                    seg["errors"] += 1
                if r["actual_credit"] is not None:
                    seg["credit"] = (seg["credit"] or 0) + float(r["actual_credit"])
                    seg["known"] += 1
                    if float(r["actual_credit"]) == 0:
                        seg["zero"] += 1
            else:
                seg = {
                    "_key": key,
                    "account": r["account_label"],
                    "model": r["model_name"],
                    "platform": r["platform"],
                    # R1.5：这段属于 low 还是 high（用户要据此判断双模式实际调用）
                    "public_mode": r["public_mode"],
                    "reason": r["choose_reason"],
                    "first_at": r["created_at"],
                    "last_at": r["created_at"],
                    "requests": 1,
                    "errors": 1 if r["status"] == "error" else 0,
                    "tokens": int(r["token_estimate"] or 0),
                    "credit": float(r["actual_credit"]) if r["actual_credit"] is not None else None,
                    # R1.3：这一段到底是「免费轮转」还是「被迫付费降级」？
                    # 判据用**真实扣费**（actual_credit），不用模型名字——名字会骗人：
                    # hy4flash/hunyuan-chat 这些在短请求下也免费，长请求下照样扣钱。
                    #   known = 拿到真实扣费的请求数
                    #   zero  = 其中扣费为 0 的请求数
                    "known": 1 if r["actual_credit"] is not None else 0,
                    "zero": 1 if (r["actual_credit"] is not None
                                  and float(r["actual_credit"]) == 0) else 0,
                }
                segments.append(seg)
        out = []
        # R1.4：免费池判定按**模型粒度**（costmodel 的 robust_free），不按单次扣费。
        # 用户拍板：扣费了就属于付费池——「短请求免费档」的长文本收费模型
        # （hy4flash/hunyuan-chat/space-bunny/glm-5.3-flash）是付费池成员，
        # 免费池成员只有 hy4flash-free 这种真免费模型。流向图的「免费轮转」
        # 视图 = 免费池成员之间的账号轮询。
        # R1.5：免费池身份统一按**官方标记**（freepool），不再用 robust_free 实测推断。
        # 用户拍板：只认官方目前标 free 的模型，避免付费模型短请求免费时被误归免费池。
        try:
            from .freepool import official_free_names

            free_pool = official_free_names()
        except Exception:
            free_pool = set()
        for i, seg in enumerate(segments):
            # 上一段 → 这一段，就是一次切换；标注「从谁切来」方便直接看
            prev = segments[i - 1] if i > 0 else None
            known = seg["known"]
            zero = seg["zero"]
            if not known:
                tier = "unknown"       # 没拿到真实扣费，不猜
            elif zero == known:
                tier = "free"          # 整段都免费（扣费全 0）
            elif zero > 0:
                tier = "mixed"         # 同一段里既有免费又有付费（长度不同）
            else:
                tier = "paid"          # 整段都真扣了钱（zero=0）
            # 模型是否属于免费池（robust_free），这是流向图过滤的正式口径
            in_pool = seg["model"] in free_pool
            out.append({
                "account": seg["account"],
                "model": seg["model"],
                "platform": seg["platform"],
                "public_mode": seg.get("public_mode", "low"),
                "reason": seg["reason"],
                "first_at": seg["first_at"],
                "last_at": seg["last_at"],
                "requests": seg["requests"],
                "errors": seg["errors"],
                "tokens": seg["tokens"],
                "credit": round(seg["credit"], 4) if seg["credit"] is not None else None,
                "tier": tier,
                "in_free_pool": in_pool,     # 模型粒度：是否真免费模型（robust_free）
                "credit_known": known,
                "switched_from": (
                    {"account": prev["account"], "model": prev["model"],
                     "platform": prev.get("platform")}
                    if prev and prev["_key"] != seg["_key"] else None
                ),
            })
        return list(reversed(out))

    # ---------- 账号 ----------
    def upsert_account(
        self,
        *,
        label: str,
        platform: str,
        credential_path: str,
        auth_status: str = "unknown",
        enabled: bool = True,
    ) -> None:
        now = time.time()
        with self._tx() as c:
            c.execute(
                """INSERT INTO accounts
                     (label, platform, credential_path, auth_status, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(label) DO UPDATE SET
                     platform=excluded.platform,
                     credential_path=excluded.credential_path,
                     auth_status=excluded.auth_status,
                     enabled=excluded.enabled,
                     updated_at=excluded.updated_at""",
                (label, platform, credential_path, auth_status, int(enabled), now, now),
            )

    def update_balance(
        self, label: str, balance: float | None, unit: str | None, checked_at: float
    ) -> None:
        with self._tx() as c:
            c.execute(
                """UPDATE accounts SET balance=?, balance_unit=?, balance_checked_at=?,
                   updated_at=? WHERE label=?""",
                (balance, unit, checked_at, time.time(), label),
            )

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def update_account_meta(
        self,
        label: str,
        *,
        source: str | None = None,
        expires_at: float | None = None,
        nickname: str | None = None,
        last_sync_at: float | None = None,
        quota: float | None = None,
        quota_checked_at: float | None = None,
        note: str | None = None,
        credential_path: str | None = None,
        platform: str | None = None,
        clear_note: bool = False,
        clear_quota: bool = False,
    ) -> None:
        """补账号元信息。只写给定的字段（None 表示不动），避免把已有值抹成空。

        `clear_note` / `clear_quota` 用来**显式清空**——P41 需要：
        WorkBuddy 账号早期被 Qoder 的同名登记覆盖过，note/quota 是脏的，
        重新同步时必须把 Qoder 留下的备注和额度清掉。
        """
        sets: list[str] = []
        vals: list[Any] = []
        for field, value in (
            ("source", source), ("expires_at", expires_at), ("nickname", nickname),
            ("last_sync_at", last_sync_at), ("quota", quota),
            ("quota_checked_at", quota_checked_at), ("note", note),
            ("credential_path", credential_path), ("platform", platform),
        ):
            if value is not None:
                sets.append(f"{field}=?")
                vals.append(value)
        if clear_note:
            sets.append("note=?")
            vals.append(None)
        if clear_quota:
            sets.append("quota=?")
            vals.append(None)
        if not sets:
            return
        sets.append("updated_at=?")
        vals.extend([time.time(), label])
        with self._tx() as c:
            c.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE label=?", vals)

    def delete_account(self, label: str) -> int:
        """删掉一行账号登记（只删登记行，不动凭据文件）。"""
        with self._tx() as c:
            cur = c.execute("DELETE FROM accounts WHERE label=?", (label,))
            return cur.rowcount

    def set_account_enabled(self, label: str, enabled: bool) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE accounts SET enabled=?, updated_at=? WHERE label=?",
                (int(enabled), time.time(), label),
            )

    # ---------- 实测探针（P34） ----------
    def put_probe(
        self,
        account_label: str,
        model_name: str,
        *,
        credit: float | None,
        tokens: int | None,
        ok: bool,
        error: str | None = None,
    ) -> None:
        """落一次探针结果。

        **失败的探针不覆盖上一次成功的测量值**（P40）。
        探针失败的常见原因是限流/网络抖动，而实测扣费是模型的属性、
        不是这次请求的属性——把它抹掉等于把「实测免费」降级成「未知」，
        调度器就会改用更贵的模型。所以失败只更新错误信息，成功才更新数值。
        """
        with self._tx() as c:
            c.execute(
                """INSERT INTO model_probes
                     (model_name, account_label, credit, tokens, checked_at, ok, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(model_name) DO UPDATE SET
                     account_label=excluded.account_label,
                     credit=CASE WHEN excluded.ok=1 THEN excluded.credit
                                 ELSE model_probes.credit END,
                     tokens=CASE WHEN excluded.ok=1 THEN excluded.tokens
                                 ELSE model_probes.tokens END,
                     checked_at=CASE WHEN excluded.ok=1 THEN excluded.checked_at
                                     ELSE model_probes.checked_at END,
                     ok=CASE WHEN excluded.ok=1 THEN 1 ELSE model_probes.ok END,
                     error=excluded.error""",
                (model_name, account_label, credit, tokens, time.time(),
                 int(ok), (error or "")[:200] or None),
            )

    def get_probe(self, account_label: str, model_name: str) -> dict[str, Any] | None:
        """按模型取实测记录（account_label 只作为「用哪个账号探的」备注）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM model_probes WHERE model_name=?", (model_name,)
            ).fetchone()
        return dict(row) if row else None

    def list_probes(self, since: float | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM model_probes"
        params: tuple = ()
        if since is not None:
            sql += " WHERE checked_at>=?"
            params = (since,)
        sql += " ORDER BY checked_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def prune_probes(self, older_than: float) -> int:
        with self._tx() as c:
            cur = c.execute("DELETE FROM model_probes WHERE checked_at < ?", (older_than,))
            return cur.rowcount

    # ---------- 价格曲线观测点（P41） ----------

    def get_pricing_points(self) -> dict[str, list[list[float]]]:
        """全部模型的价格观测点：{model_name: [[tokens, credit], ...]}。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT model_name, points FROM model_pricing"
            ).fetchall()
        out: dict[str, list[list[float]]] = {}
        for r in rows:
            try:
                pts = json.loads(r["points"])
            except (TypeError, ValueError):
                pts = []
            if isinstance(pts, list):
                out[r["model_name"]] = pts
        return out

    def merge_pricing_points(
        self, model_name: str, points: list[list[float]]
    ) -> list[list[float]]:
        """把新观测点并进某个模型的曲线，返回合并后的点集。

        合并规则：同一 token 数取**最大** credit（保守——宁可高估成本，
        也不能在「实际扣了 0.01」时估成 0）。抽稀上限交给 costmodel 决定。
        """
        from .costmodel import merge_points

        with self._tx() as c:
            row = c.execute(
                "SELECT points FROM model_pricing WHERE model_name=?", (model_name,)
            ).fetchone()
            try:
                existing = json.loads(row["points"]) if row else []
            except (TypeError, ValueError):
                existing = []
            if not isinstance(existing, list):
                existing = []
            merged = merge_points(existing, points)
            c.execute(
                """INSERT INTO model_pricing (model_name, points, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(model_name) DO UPDATE SET
                     points=excluded.points, updated_at=excluded.updated_at""",
                (model_name, json.dumps(merged), time.time()),
            )
        return merged

    def clear_pricing_points(self, model_name: str | None = None) -> int:
        """清空观测点（后台「重置实测数据」用）。"""
        with self._tx() as c:
            if model_name:
                cur = c.execute(
                    "DELETE FROM model_pricing WHERE model_name=?", (model_name,)
                )
            else:
                cur = c.execute("DELETE FROM model_pricing")
            return cur.rowcount

    # ---------- 冷却（P35） ----------
    def set_cooldown(
        self, account_label: str, model_name: str, until_at: float, reason: str = ""
    ) -> None:
        with self._tx() as c:
            c.execute(
                """INSERT INTO cooldowns
                     (account_label, model_name, until_at, reason, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(account_label, model_name) DO UPDATE SET
                     until_at=excluded.until_at, reason=excluded.reason,
                     created_at=excluded.created_at""",
                (account_label, model_name, float(until_at), (reason or "")[:200],
                 time.time()),
            )

    def clear_cooldown(self, account_label: str, model_name: str) -> None:
        with self._tx() as c:
            c.execute(
                "DELETE FROM cooldowns WHERE account_label=? AND model_name=?",
                (account_label, model_name),
            )

    def list_cooldowns(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cooldowns WHERE until_at > ? ORDER BY until_at", (now,)
            ).fetchall()
        return [dict(r) for r in rows]

    def purge_cooldowns(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._tx() as c:
            cur = c.execute("DELETE FROM cooldowns WHERE until_at <= ?", (now,))
            return cur.rowcount

    # ---------- 轮转计数（P35 / R2.35 滑动窗口） ----------
    def rotation_counts(self, model_name: str) -> dict[str, int]:
        """最近窗口内每个账号对该模型的使用次数（R2.35）。

        旧版读 rotation 历史累计（永久只增），一旦拉开差距就长期偏向少数账号
        （线上 10 个 WB 账号只有 5 个被轮询到）。现在读 rotation_events 的
        滑动窗口计数：窗口滑过即归零，各账号公平轮转。配置窗口≤0 时退回累计口径。

        签名保持不变（scheduler 与测试都只传 model_name）。返回结构与旧版一致。
        """
        window = _rotation_window_seconds()
        if window <= 0:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT account_label, n FROM rotation WHERE model_name=?", (model_name,)
                ).fetchall()
            return {r["account_label"]: int(r["n"] or 0) for r in rows}
        cutoff = time.time() - window
        with self._lock:
            rows = self._conn.execute(
                """SELECT account_label, COUNT(*) AS n FROM rotation_events
                   WHERE model_name=? AND used_at>=?
                   GROUP BY account_label""",
                (model_name, cutoff),
            ).fetchall()
        return {r["account_label"]: int(r["n"] or 0) for r in rows}

    def bump_rotation(self, account_label: str, model_name: str) -> int:
        """用一次：累计表 +1，并记一条带时间戳的窗口事件；返回累计计数。"""
        now = time.time()
        window = _rotation_window_seconds()
        # 保留窗口 3 倍或至少 1 小时的事件，避免表无限增长，又给「调大窗口」留余量。
        keep_for = max(window * 3, 3600.0)
        with self._tx() as c:
            c.execute(
                """INSERT INTO rotation(account_label, model_name, n, updated_at)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(account_label, model_name) DO UPDATE SET
                     n = n + 1, updated_at=excluded.updated_at""",
                (account_label, model_name, now),
            )
            c.execute(
                "INSERT INTO rotation_events(account_label, model_name, used_at) VALUES (?,?,?)",
                (account_label, model_name, now),
            )
            c.execute("DELETE FROM rotation_events WHERE used_at < ?", (now - keep_for,))
            row = c.execute(
                "SELECT n FROM rotation WHERE account_label=? AND model_name=?",
                (account_label, model_name),
            ).fetchone()
        return int(row["n"] or 0) if row else 1

    # ---------- 调度日志（P37） ----------
    def log_schedule(self, event: str, detail: str = "", level: str = "info") -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO schedule_log(created_at, level, event, detail) VALUES (?,?,?,?)",
                (time.time(), level, event[:80], (detail or "")[:500]),
            )

    # ---------- 凭据续期失败状态（R2.33） ----------
    def renew_result(self, account_label: str, platform: str, *,
                     ok: bool) -> dict[str, Any]:
        """记一次续期结果：成功清零连败；失败 +1。返回更新后的状态。

        用 UPSERT 保证并发安全。notified_at 不动——发过提醒后即使继续失败，
        也由调用方按 notified_at 判断是否需要再发（避免每 6 小时一封轰炸）。
        """
        now = time.time()
        streak = 0 if ok else 1
        with self._tx() as c:
            c.execute(
                """INSERT INTO renew_state(account_label, platform, fail_streak,
                       last_fail_at, notified_at) VALUES (?,?,?,?,NULL)
                   ON CONFLICT(account_label, platform) DO UPDATE SET
                     fail_streak = CASE WHEN ? = 1
                                        THEN renew_state.fail_streak + 1 ELSE 0 END,
                     last_fail_at = CASE WHEN ? = 1
                                        THEN ? ELSE renew_state.last_fail_at END""",
                (account_label, platform, streak, now,
                 streak, streak, now),
            )
            row = c.execute(
                "SELECT fail_streak, last_fail_at, notified_at FROM renew_state "
                "WHERE account_label=? AND platform=?", (account_label, platform),
            ).fetchone()
        return {
            "fail_streak": int(row["fail_streak"] or 0),
            "last_fail_at": row["last_fail_at"],
            "notified_at": row["notified_at"],
        }

    def mark_renew_notified(self, account_label: str, platform: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE renew_state SET notified_at=? WHERE account_label=? AND platform=?",
                (time.time(), account_label, platform),
            )

    def renew_states(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM renew_state").fetchall()
        return [dict(r) for r in rows]

    def clear_renew_state(self, account_label: str, platform: str) -> None:
        """人工重新扫码后清掉续期失败记录，让账号行的红色告警立刻消失。

        口径与 renew_result 一致：production 写的是 "WorkBuddy"/"Qoder"，
        测试/部分调用写小写——这里用 lower() 比对，两种都清掉。
        """
        with self._tx() as c:
            c.execute(
                "DELETE FROM renew_state WHERE lower(account_label)=lower(?) "
                "AND lower(platform)=lower(?)",
                (account_label, platform),
            )

    def list_schedule_log(self, limit: int = 60) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM schedule_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def prune_schedule_log(self, keep: int = 500) -> int:
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM schedule_log WHERE id NOT IN "
                "(SELECT id FROM schedule_log ORDER BY id DESC LIMIT ?)",
                (keep,),
            )
            return cur.rowcount

    # ---------- 模型目录 ----------
    def upsert_models(self, platform: str, models: list[dict[str, Any]]) -> None:
        now = time.time()
        with self._tx() as c:
            for m in models:
                c.execute(
                    """INSERT INTO models
                         (platform, model_name, public_name, is_free, price_factor,
                          promotion_until, tools, image, context_window, raw, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(platform, model_name) DO UPDATE SET
                         public_name=excluded.public_name,
                         is_free=excluded.is_free,
                         price_factor=excluded.price_factor,
                         promotion_until=excluded.promotion_until,
                         tools=excluded.tools,
                         image=excluded.image,
                         context_window=excluded.context_window,
                         raw=excluded.raw,
                         fetched_at=excluded.fetched_at""",
                    (
                        platform,
                        m.get("model_name", ""),
                        m.get("public_name"),
                        None if m.get("is_free") is None else int(bool(m["is_free"])),
                        m.get("price_factor"),
                        m.get("promotion_until"),
                        None if m.get("tools") is None else int(bool(m["tools"])),
                        None if m.get("image") is None else int(bool(m["image"])),
                        m.get("context_window"),
                        json.dumps(m.get("raw", {}), ensure_ascii=False),
                        now,
                    ),
                )

    def list_models(self, platform: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if platform:
                rows = self._conn.execute(
                    "SELECT * FROM models WHERE platform=? ORDER BY id", (platform,)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM models ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # ---------- 账号消耗（P18） ----------
    def usage_by_account(self, days: int = 30) -> dict[str, dict[str, Any]]:
        """按账号聚合网关侧消耗（只统计能取到真实扣费的记录，不估算冒充）。

        返回 {account_label: {total, today, d7, d30, requests, last_at, daily:[{date, credit}]}}
        日期按服务器本地时区切，和页面上看到的时间一致。
        """
        since = time.time() - days * 86400
        with self._lock:
            rows = self._conn.execute(
                "SELECT account_label, created_at, actual_credit, status FROM requests "
                "WHERE created_at >= ? AND account_label IS NOT NULL",
                (since,),
            ).fetchall()
        now = time.time()
        today_key = time.strftime("%Y-%m-%d", time.localtime(now))
        buckets: dict[str, dict[str, Any]] = {}
        for r in rows:
            label = r["account_label"]
            day = time.strftime("%Y-%m-%d", time.localtime(r["created_at"]))
            b = buckets.setdefault(label, {
                "total": 0.0, "today": 0.0, "d7": 0.0, "d30": 0.0,
                "requests": 0, "last_at": None, "daily": {},
            })
            credit = float(r["actual_credit"] or 0.0)
            b["requests"] += 1
            b["total"] += credit
            b["d30"] += credit
            if r["created_at"] >= now - 7 * 86400:
                b["d7"] += credit
            if day == today_key:
                b["today"] += credit
            b["daily"][day] = b["daily"].get(day, 0.0) + credit
            if b["last_at"] is None or r["created_at"] > b["last_at"]:
                b["last_at"] = r["created_at"]
        # 补齐最近 7 天（没流量的日子显式补 0，图才连得上）
        days7 = [
            time.strftime("%Y-%m-%d", time.localtime(now - i * 86400)) for i in range(6, -1, -1)
        ]
        for b in buckets.values():
            b["daily"] = [{"date": d, "credit": round(b["daily"].get(d, 0.0), 4)} for d in days7]
            for k in ("total", "today", "d7", "d30"):
                b[k] = round(b[k], 4)
        return buckets

    def usage_rows(self, account_label: str, limit: int = 20) -> list[dict[str, Any]]:
        """某账号最近的请求（用于展开后的明细）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT created_at, model_name, status, latency_ms, token_estimate, actual_credit "
                "FROM requests WHERE account_label=? ORDER BY id DESC LIMIT ?",
                (account_label, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 额度快照 + 变化追踪（R2.19） ----------
    def record_quota_snapshot(
        self, account_label: str, platform: str, *, available: float | None,
        quota_total: float | None, user_type: str | None, exceeded: bool | None,
        claim_active: bool | None = None, ts: float | None = None,
    ) -> None:
        """记一条「当前还有多少积分 + 活动是否可领」。available=None（查不到）也记，
        但标 NULL——绝不用 0 冒充「查不到」，否则会误报「额度清零」。同一秒只留一条。
        `ts` 可注入（采集时刻），缺省用当前时间。"""
        if account_label is None:
            return
        ts = float(round(time.time())) if ts is None else float(ts)
        with self._tx() as c:
            c.execute(
                """INSERT OR REPLACE INTO quota_snapshots
                   (account_label, ts, platform, available, quota_total, user_type,
                    exceeded, claim_active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (account_label, ts, platform, available, quota_total, user_type,
                 None if exceeded is None else int(bool(exceeded)),
                 None if claim_active is None else int(bool(claim_active))),
            )

    def latest_quota(self, account_label: str) -> dict[str, Any] | None:
        """某账号最近一条额度快照（变化检测的基线）。"""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM quota_snapshots WHERE account_label=? "
                "ORDER BY ts DESC LIMIT 1", (account_label,),
            ).fetchone()
        return dict(r) if r else None

    def quota_history(self, account_label: str, limit: int = 168) -> list[dict[str, Any]]:
        """某账号最近 N 条额度快照（默认 168 = 一周每小时），供曲线用。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM quota_snapshots WHERE account_label=? "
                "ORDER BY ts DESC LIMIT ?", (account_label, int(limit)),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ---------- 账单快照 + 按小时用量（P27） ----------
    def record_usage_snapshot(self, account_label: str, used: float | None) -> None:
        """记一条「当前已用总量」。同一秒重复刷新只留一条，避免刷屏。

        只应在真实取到账单时调用；取不到（token 过期/网络失败）就别写，
        否则会把「读取失败」当成「没消耗」。
        """
        if used is None or account_label is None:
            return
        ts = float(round(time.time()))
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO usage_snapshots(account_label, ts, used) VALUES (?, ?, ?)",
                (account_label, ts, float(used)),
            )

    def usage_today(self, account_label: str) -> dict[str, Any]:
        """今天消耗了多少（平台账单口径）。

        算法：以「今天 00:00 之前最后一条快照」为基线，最新快照减它。
        今天还没有基线（比如昨天没开过后台）时退回今天第一条，并在 `since`
        里给出起点，页面上写清「自 HH:MM 起」，不假装是完整一天。
        """
        now = time.time()
        lt = time.localtime(now)
        day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        with self._lock:
            prev = self._conn.execute(
                "SELECT used, ts FROM usage_snapshots WHERE account_label=? AND ts<? "
                "ORDER BY ts DESC LIMIT 1",
                (account_label, day_start),
            ).fetchone()
            rows = self._conn.execute(
                "SELECT used, ts FROM usage_snapshots WHERE account_label=? AND ts>=? ORDER BY ts",
                (account_label, day_start),
            ).fetchall()
        if not rows:
            return {
                "delta": None, "base": None, "latest": None, "since": None,
                "samples": 0, "note": "今天还没有读到账单",
            }
        latest = float(rows[-1]["used"])
        base_row = prev if prev else rows[0]
        base = float(base_row["used"])
        return {
            "delta": round(max(0.0, latest - base), 2),
            "base": round(base, 2),
            "latest": round(latest, 2),
            "since": float(base_row["ts"]),
            "samples": len(rows),
            "stale_sec": round(now - float(rows[-1]["ts"])),
            "whole_day": prev is not None,
        }

    def credit_today(self) -> float:
        """当日全部请求的真实扣费之和（拿不到 credit 的记 0，不估算）。"""
        day = time.strftime("%Y-%m-%d")
        with self._lock:
            row = self._conn.execute(
                """SELECT coalesce(sum(actual_credit), 0) AS c
                   FROM requests WHERE date(created_at,'unixepoch','localtime')=?""",
                (day,),
            ).fetchone()
        return float(row["c"] or 0.0) if row else 0.0

    # R1 双模式统计：`public_model` 的过滤条件。历史数据是 personal-agent
    # （low 模式的直接前身，配置镜像），按建议归入 low，保证历史曲线不断档。
    # 返回 (sql_fragment, params)；model in (None,"all") 时不过滤。
    @staticmethod
    def _usage_model_filter(model: str | None) -> tuple[str, list[Any]]:
        m = (model or "").strip().lower()
        if m in ("", "all"):
            return "", []
        if m == "low":
            return " AND public_model IN ('low','personal-agent')", []
        if m == "high":
            return " AND public_model='high'", []
        return " AND public_model=?", [model]

    def model_call_counts(self, days: int = 7) -> dict[str, dict[str, int]]:
        """近 N 天各模型的实际调用次数，按 low/high 分模式（R2.5）。

        用于后台 low/high 模型列表按「实际调用情况」排序——页面顺序不再是
        字典序或预览长度的 cost 打平，而是用户真实用到的先后。
        返回 {model_name: {"low": n, "high": n, "total": n}}。
        """
        since = time.time() - max(1, int(days)) * 86400
        with self._lock:
            rows = self._conn.execute(
                """SELECT model_name,
                          CASE WHEN public_model='high' THEN 'high' ELSE 'low' END AS mode,
                          count(*) AS n
                   FROM requests
                   WHERE created_at>=?
                   GROUP BY model_name, mode""",
                (since,),
            ).fetchall()
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            d = out.setdefault(r["model_name"], {"low": 0, "high": 0, "total": 0})
            d[r["mode"]] = r["n"]
            d["total"] += r["n"]
        return out

    def usage_day(self, account_label: str | None, day: str | None = None,
                  model: str | None = None) -> dict[str, Any]:
        """某一天的 24 小时分布 + 模型分布 + 分时明细入口（网关请求口径）。

        account_label=None = 全账号汇总（R1.1）。
        model=None/"all" = low+high 全叠加；"low"/"high" = 单模式过滤，
        历史 personal-agent 归入 low。

        平台账单只给总量，所以这里的 24 小时是「经过网关的请求」——
        每条带模型名、耗时、Token 估算，能回答「几点用的什么模型」。
        积分列只在真的拿到上游 credit 时才有值，拿不到就是 None，不拿估算凑数。
        """
        day = day or time.strftime("%Y-%m-%d")
        where, mparams = self._usage_model_filter(model)
        acct_sql = "account_label=? AND " if account_label is not None else ""
        params: list[Any] = ([account_label] if account_label is not None else []) + mparams + [day]
        with self._lock:
            rows = self._conn.execute(
                """SELECT strftime('%H', datetime(created_at,'unixepoch','localtime')) AS h,
                          model_name,
                          CASE WHEN public_model='high' THEN 'high' ELSE 'low' END AS public_mode,
                          count(*) AS n,
                          sum(coalesce(actual_credit, 0)) AS credit,
                          sum(CASE WHEN actual_credit IS NOT NULL THEN 1 ELSE 0 END) AS known,
                          sum(coalesce(token_estimate, 0)) AS tokens,
                          avg(coalesce(latency_ms, 0)) AS latency,
                          sum(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors
                   FROM requests
                   WHERE """ + acct_sql + "date(created_at,'unixepoch','localtime')=?" + where +
                " GROUP BY h, model_name, public_mode",
                tuple(params),
            ).fetchall()
        hours: list[dict[str, Any]] = [
            {"hour": f"{h:02d}", "requests": 0, "credit": 0.0, "credit_requests": 0,
             "tokens": 0, "latency": 0.0, "errors": 0, "models": [],
             # R1.1：按模式分量，供前端「全部/low/high」本地切换，不重复请求
             "by_mode": {"low": {"requests": 0, "credit": 0.0, "tokens": 0},
                         "high": {"requests": 0, "credit": 0.0, "tokens": 0}}}
            for h in range(24)
        ]
        for r in rows:
            h = int(r["h"] or 0)
            b = hours[h]
            n = int(r["n"] or 0)
            credit = float(r["credit"] or 0)
            tokens = int(r["tokens"] or 0)
            b["requests"] += n
            b["credit"] += credit
            # P29：上游只在少数响应里回传扣费，所以要单独给「知道金额的请求数」，
            # 否则页面上那 0.02 会被当成网关今天一共花了多少。
            b["credit_requests"] += int(r["known"] or 0)
            b["tokens"] += tokens
            b["errors"] += int(r["errors"] or 0)
            b["latency"] = max(b["latency"], float(r["latency"] or 0))
            mode = r["public_mode"] or "low"
            bm = b["by_mode"][mode]
            bm["requests"] += n
            bm["credit"] += credit
            bm["tokens"] += tokens
            b["models"].append({
                "name": r["model_name"] or "unknown",
                "public_mode": mode,
                "requests": n,
                "credit": round(credit, 4),
                # R2.53：模型分布随指标切换显示 token，聚合要带上 token 分量。
                "tokens": tokens,
            })
        total_requests = sum(b["requests"] for b in hours)
        total_errors = sum(b["errors"] for b in hours)
        total_tokens = sum(b["tokens"] for b in hours)
        total_credit_requests = sum(b["credit_requests"] for b in hours)
        credit_known = any(b["credit"] for b in hours)
        for b in hours:
            b["credit"] = round(b["credit"], 4) if b["credit"] else None
            b["latency"] = round(b["latency"]) if b["latency"] else None
            # R1.1：by_mode 分量保留精度（前端本地切换 low/high 用）
            for bm in b["by_mode"].values():
                bm["credit"] = round(bm["credit"], 4)
            b["models"].sort(key=lambda m: m["requests"], reverse=True)
        models: dict[str, dict[str, Any]] = {}
        for b in hours:
            for m in b["models"]:
                cur = models.setdefault(m["name"], {
                    "name": m["name"], "requests": 0, "credit": 0.0, "tokens": 0,
                    "public_mode": m.get("public_mode", "low"),
                })
                cur["requests"] += m["requests"]
                cur["credit"] += m["credit"] or 0
                cur["tokens"] += m.get("tokens", 0)
        order = sorted(models.values(), key=lambda m: m["requests"], reverse=True)
        return {
            "day": day,
            "requests": total_requests,
            "errors": total_errors,
            "tokens": total_tokens,
            "credit": round(sum(m["credit"] for m in order), 4) if credit_known else None,
            "credit_requests": total_credit_requests,
            "hours": hours,
            "models": order,
        }

    def usage_hour_detail(
        self, account_label: str | None, day: str, hour: str,
        limit: int = 200, model: str | None = None,
    ) -> list[dict[str, Any]]:
        """某一小时的请求明细（点柱子展开用）。account_label=None=全账号汇总。"""
        where, mparams = self._usage_model_filter(model)
        acct_sql = "account_label=? AND " if account_label is not None else ""
        params: list[Any] = ([account_label] if account_label is not None else []) + [day, hour] + mparams + [limit]
        with self._lock:
            rows = self._conn.execute(
                """SELECT created_at, model_name, status, latency_ms, token_estimate,
                          actual_credit, stream, public_model, platform, account_label
                   FROM requests
                   WHERE """ + acct_sql +
                "date(created_at,'unixepoch','localtime')=?\n"
                "                     AND strftime('%H', datetime(created_at,'unixepoch','localtime'))=?"
                + where +
                "                   ORDER BY created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 后台会话（P17） ----------
    def create_session(
        self, sid: str, *, ip: str, ua: str, ttl: float
    ) -> None:
        now = time.time()
        with self._tx() as c:
            c.execute(
                "INSERT INTO admin_sessions (sid, created_at, last_seen, expires_at, ip, ua, revoked)"
                " VALUES (?, ?, ?, ?, ?, ?, 0)",
                (sid, now, now, now + ttl, ip, ua[:300] if ua else ""),
            )

    def get_session(self, sid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM admin_sessions WHERE sid=?", (sid,)
            ).fetchone()
        return dict(row) if row else None

    def touch_session(self, sid: str, *, expires_at: float, min_gap: float = 30.0) -> None:
        """滑动续期。只在「上次记录 > min_gap 秒」时写库，避免每个请求都落盘。"""
        now = time.time()
        with self._tx() as c:
            c.execute(
                "UPDATE admin_sessions SET last_seen=?, expires_at=? "
                "WHERE sid=? AND last_seen < ?",
                (now, expires_at, sid, now - min_gap),
            )

    def revoke_session(self, sid: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE admin_sessions SET revoked=1 WHERE sid=?", (sid,))

    def purge_sessions(self) -> int:
        """清理过期/已撤销会话（登录时顺手调用，保持表很小）。"""
        now = time.time()
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM admin_sessions WHERE expires_at < ? OR revoked=1", (now,)
            )
            return cur.rowcount

    def list_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT sid, created_at, last_seen, expires_at, ip, ua FROM admin_sessions "
                "WHERE revoked=0 AND expires_at > ? ORDER BY last_seen DESC LIMIT ?",
                (time.time(), limit),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["sid_prefix"] = (d.pop("sid") or "")[:8]   # 只给前缀，不暴露完整 sid
            out.append(d)
        return out

    # ---------- 登录审计 ----------
    def record_auth_event(
        self, *, ip: str, ua: str, path: str, result: str, reason: str = ""
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO admin_auth_events (created_at, ip, ua, path, result, reason)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), ip, (ua or "")[:300], (path or "")[:200], result, reason[:200]),
            )

    def count_failures(self, ip: str, since: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM admin_auth_events "
                "WHERE ip=? AND result='deny' AND created_at>=?",
                (ip, since),
            ).fetchone()
        return int(row["n"] or 0)

    def last_event_at(self, ip: str, result: str, since: float = 0.0) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(created_at) AS t FROM admin_auth_events "
                "WHERE ip=? AND result=? AND created_at>=?",
                (ip, result, since),
            ).fetchone()
        return float(row["t"]) if row and row["t"] else None

    def list_auth_events(self, *, result: str | None = None, limit: int = 12) -> list[dict[str, Any]]:
        sql = "SELECT created_at, ip, ua, path, result, reason FROM admin_auth_events"
        params: tuple = ()
        if result:
            sql += " WHERE result=?"
            params = (result,)
        sql += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, params + (limit,)).fetchall()
        return [dict(r) for r in rows]

    def failure_ips(self, since: float, limit: int = 8) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ip, COUNT(*) AS n, MAX(created_at) AS last_at "
                "FROM admin_auth_events WHERE result='deny' AND created_at>=? "
                "GROUP BY ip ORDER BY n DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 键值配置（P20） ----------
    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        return str(row["value"]) if row and row["value"] is not None else default

    def set_setting(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, time.time()),
            )

    # ---------- 邮件登录链接（P20） ----------
    def create_magic(self, token_id: str, *, ip: str, ua: str, ttl: float) -> None:
        now = time.time()
        with self._tx() as c:
            c.execute("DELETE FROM admin_magic WHERE expires_at < ? OR used_at IS NOT NULL",
                      (now - 3600,))
            c.execute(
                "INSERT INTO admin_magic (id, created_at, expires_at, used_at, ip, ua)"
                " VALUES (?, ?, ?, NULL, ?, ?)",
                (token_id, now, now + ttl, ip, (ua or "")[:300]),
            )

    def get_magic(self, token_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM admin_magic WHERE id=?", (token_id,)
            ).fetchone()
        return dict(row) if row else None

    def use_magic(self, token_id: str) -> bool:
        """原子消费：只有「未用过且未过期」的才能改成已用，返回是否抢到。"""
        now = time.time()
        with self._tx() as c:
            cur = c.execute(
                "UPDATE admin_magic SET used_at=? WHERE id=? AND used_at IS NULL AND expires_at > ?",
                (now, token_id, now),
            )
            return cur.rowcount > 0

    def count_magic(self, ip: str, since: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM admin_magic WHERE ip=? AND created_at>=?",
                (ip, since),
            ).fetchone()
        return int(row["n"] or 0)

    def list_magic(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT created_at, expires_at, used_at, ip FROM admin_magic "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


# 全局单例（测试时可用临时路径另建 Store 实例）
store = Store()
