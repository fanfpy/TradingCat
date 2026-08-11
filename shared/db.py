#!/usr/bin/env python3
"""
SQLite 访问层 — 交易系统 v4.0 StateRepository（架构 D-1 / D-13）
================================================================
核心数据层：行情缓存 + 数据溯源 + 生命周期状态 + 组合账本 + 执行链 + 审计。

架构 v4.0 schema 分层：
- research 层：bars / data_manifest / lifecycle / research_archive（行情与策略研究）
- trading 层：trading_account / trading_universe / trading_execution_plan /
  trading_confirmation / trading_order_intent / trading_broker_order /
  trading_fill / trading_market_state（账户/计划/订单/成交，接实盘前必须 P3 验收）
- audit 层：audit_log（D-10 lineage，回答"为什么买 NVDA"）

SQLite 使用纪律（D-1，按交易状态库对待）：
- WAL mode / foreign_keys=ON / busy_timeout=5000ms / 短事务
- 每进程独立 connection，禁止跨线程共享 connection
- migration version（schema_version 表）
- 在线备份禁止直接 cp（WAL 未 checkpoint 会丢数据）：用 Backup API / VACUUM INTO

设计原则（延续 v3.0）：
- bars 表存固定日线 OHLCV，主键 (symbol, ts)
- data_manifest 记录每份数据来源/日期范围/SHA-256（切换供应商整段替换）
- lifecycle 表是研究生命周期状态的唯一真相源（程序写入，禁止人工改）
- 监听范围由 watchlist_item + monitoring_subscription 决定；旧 JSON 只迁移
- portfolio 表记录持仓/止损/仓位状态
- 所有 DB 访问统一走本文件（StateRepository 收口 D-13），业务层禁止直接 sqlite3.connect()
"""

import json
import os
import sqlite3
import hashlib
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any

from shared.config import get_config

# v5 将业务核心状态与执行状态拆成两个物理 store。TRADING_DB 继续作为
# TRADING_CORE_DB 的兼容别名，避免已有部署在升级时突然换库。
_CONFIG = get_config()
CORE_DB_PATH = _CONFIG.database.core_path
EXECUTION_DB_PATH = _CONFIG.database.execution_path
DB_PATH = CORE_DB_PATH

SCHEMA = """
-- ============ research 层（v3.0 原有） ============

-- 行情缓存表
CREATE TABLE IF NOT EXISTS bars (
    symbol  TEXT NOT NULL,
    ts      TEXT NOT NULL,           -- 交易日 (YYYY-MM-DD, 交易所本地日)
    open    REAL NOT NULL,
    high    REAL NOT NULL,
    low     REAL NOT NULL,
    close   REAL NOT NULL,
    volume  REAL NOT NULL,
    source  TEXT NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_bars_symbol ON bars(symbol);

-- 数据溯源表
CREATE TABLE IF NOT EXISTS data_manifest (
    symbol         TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    fetched_at     TEXT NOT NULL,
    last_completed TEXT NOT NULL,
    date_start     TEXT NOT NULL,
    date_end       TEXT NOT NULL,
    bar_count      INTEGER NOT NULL,
    sha256         TEXT NOT NULL,
    adjustment_mode TEXT NOT NULL DEFAULT 'UNKNOWN',
    corporate_actions_status TEXT NOT NULL DEFAULT 'UNKNOWN'
);

-- 生命周期状态表（含 shadow：验证方向成立但统计量不足，不可交易）
CREATE TABLE IF NOT EXISTS lifecycle (
    symbol        TEXT PRIMARY KEY,
    status        TEXT NOT NULL,      -- candidate|backtesting|research_only|shadow|verified|live|degraded|suspended|removed
    fail_count    INTEGER NOT NULL DEFAULT 0,
    last_evidence_hash TEXT,
    score         REAL,
    params_json   TEXT,
    updated_at    TEXT NOT NULL
);

-- 持仓/组合账本（模拟账本，非券商持仓）
CREATE TABLE IF NOT EXISTS portfolio (
    symbol       TEXT PRIMARY KEY,
    entry_price  REAL NOT NULL,
    entry_ts     TEXT NOT NULL,
    quantity     REAL NOT NULL,
    stop_price   REAL NOT NULL,
    peak_high    REAL NOT NULL,
    batch        INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL
);

-- 研究归档 (degraded/removed 保留全部证据)
CREATE TABLE IF NOT EXISTS research_archive (
    symbol       TEXT NOT NULL,
    status       TEXT NOT NULL,       -- degraded|removed
    params_json  TEXT,
    score        REAL,
    evidence_json TEXT,               -- 验证证据（OOS 折结果等）
    archived_at  TEXT NOT NULL,
    PRIMARY KEY (symbol, status)
);
"""

SCHEMA_V4 = """
-- ============ 架构 v4.0 分层扩展（P1 DataHub） ============

-- migration version（D-1 SQLite 纪律）
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (4, '2026-08-08T00:00:00Z');
INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (5, '2026-08-08T00:00:00Z');

-- ============ trading 层 ============

-- 账户状态（D-7：sync_status 非 SYNCED → PreTradeRisk 必须 REJECT 已批准订单）
CREATE TABLE IF NOT EXISTS trading_account (
    account_id   TEXT PRIMARY KEY,
    sync_status  TEXT NOT NULL DEFAULT 'UNKNOWN',  -- SYNCED|STALE|RECONCILING|MISMATCH|UNKNOWN
    cash         REAL,
    buying_power REAL,
    nav          REAL,
    updated_at   TEXT NOT NULL,
    raw_json     TEXT
);

-- UniverseSnapshot（D-5 防幸存者偏差 / quota 管理；记录每标的进入 universe 的时间与状态）
CREATE TABLE IF NOT EXISTS trading_universe (
    symbol      TEXT PRIMARY KEY,
    source      TEXT NOT NULL,          -- portfolio|watchlist|research|manual
    added_at    TEXT NOT NULL,
    status      TEXT NOT NULL,          -- active|suspended|removed
    snapshot_ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_universe_source ON trading_universe(source);

-- ExecutionPlan（D-3：不可变计划，plan_hash 覆盖 account_id+execution_mode+orders+expires_at）
CREATE TABLE IF NOT EXISTS trading_execution_plan (
    plan_id        TEXT PRIMARY KEY,
    account_id     TEXT NOT NULL,
    execution_mode TEXT NOT NULL,       -- DRY_RUN|LIVE
    expires_at     TEXT NOT NULL,
    plan_hash      TEXT NOT NULL,
    orders_json    TEXT NOT NULL,       -- PlanOrder[] 完整快照（不可变）
    status         TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING|CONFIRMED|CONSUMED|EXPIRED|REJECTED|CANCELLED
    created_at     TEXT NOT NULL
);

-- Confirmation（D-3/D-12：票据，单次消费；身份字段约束谁能批准）
CREATE TABLE IF NOT EXISTS trading_confirmation (
    confirmation_id TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL,
    plan_hash       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING|APPROVED|CONSUMED|EXPIRED|REJECTED|CANCELLED
    approved_by     TEXT,               -- 例: owner
    approval_channel TEXT,              -- 例: cli|wechat
    approval_nonce  TEXT UNIQUE,        -- 防 replay
    approved_at     TEXT,
    expires_at      TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_confirmation_plan ON trading_confirmation(plan_id);

-- OrderIntent（D-9 幂等：plan_id+plan_order_id UNIQUE + client_request_id UNIQUE）
CREATE TABLE IF NOT EXISTS trading_order_intent (
    intent_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    client_request_id TEXT NOT NULL UNIQUE,
    plan_id          TEXT NOT NULL,
    plan_order_id    TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,     -- BUY|SELL
    quantity         REAL NOT NULL,
    order_type       TEXT NOT NULL,     -- MARKET|LIMIT
    reference_price  REAL,
    max_slippage_bps REAL,
    strategy_version_id INTEGER,
    confirmation_id  TEXT,
    status           TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING|SUBMITTED|REJECTED|FILLED|CANCELLED|UNKNOWN
    broker_order_id  TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE (plan_id, plan_order_id)
);
CREATE INDEX IF NOT EXISTS idx_intent_plan ON trading_order_intent(plan_id);

-- BrokerOrder（券商原始订单快照）
CREATE TABLE IF NOT EXISTS trading_broker_order (
    broker_order_id TEXT PRIMARY KEY,
    intent_id       INTEGER NOT NULL REFERENCES trading_order_intent(intent_id),
    raw_json        TEXT,
    updated_at      TEXT NOT NULL
);

-- Fill（成交）
CREATE TABLE IF NOT EXISTS trading_fill (
    fill_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_order_id TEXT NOT NULL,
    intent_id       INTEGER NOT NULL REFERENCES trading_order_intent(intent_id),
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    quantity        REAL NOT NULL,
    price           REAL NOT NULL,
    filled_at       TEXT NOT NULL
);

-- MarketState（quote 新鲜度，D-8 PreTradeRisk 输入）
CREATE TABLE IF NOT EXISTS trading_market_state (
    symbol           TEXT PRIMARY KEY,
    quote_at         TEXT NOT NULL,
    price            REAL NOT NULL,
    max_age_seconds  INTEGER NOT NULL DEFAULT 300,
    updated_at       TEXT NOT NULL
);

-- StrategyVersion 版本固化（R1#11：git_commit/code_hash/data_version/params/WF report）
CREATE TABLE IF NOT EXISTS strategy_version (
    version_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    git_commit    TEXT,                -- 代码版本（git rev-parse HEAD）
    code_hash     TEXT,                -- 策略代码 hash（可用文件 hash）
    data_version  TEXT,                -- 数据版本（data_manifest.sha256）
    params_json   TEXT,                -- 参数快照
    wf_report_json TEXT,              -- Walk-Forward 报告快照
    oos_stats_json TEXT,              -- OOS 成交聚合统计（PositionSizer 的唯一证据）
    status        TEXT NOT NULL,      -- 对应 lifecycle 状态
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sv_symbol ON strategy_version(symbol);

-- ============ audit 层（D-10 lineage） ============
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    event        TEXT NOT NULL,   -- SIGNAL|PLAN_CREATED|CONFIRMATION|PRETRADE|ORDER_INTENT|BROKER_ORDER|FILL|RECONCILE|ACCOUNT_SYNC|UNIVERSE_SNAPSHOT
    entity_type  TEXT,
    entity_id    TEXT,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

-- ============ v4 DataHub / PIT / 历史 Universe ============

-- 历史股票池快照头；同一来源同一 as_of_date 只有一个不可变快照。
CREATE TABLE IF NOT EXISTS universe_snapshot (
    snapshot_id   TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    as_of_date    TEXT NOT NULL,
    captured_at   TEXT NOT NULL,
    metadata_json TEXT,
    UNIQUE(source, as_of_date)
);

CREATE TABLE IF NOT EXISTS universe_member (
    snapshot_id TEXT NOT NULL REFERENCES universe_snapshot(snapshot_id) ON DELETE CASCADE,
    symbol      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    exchange    TEXT,
    sector      TEXT,
    listed_at   TEXT,
    delisted_at TEXT,
    PRIMARY KEY(snapshot_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_universe_member_symbol ON universe_member(symbol);

-- 财报/基本面采用 point-in-time 语义：available_at 之后研究流程才可见。
CREATE TABLE IF NOT EXISTS fundamental_snapshot (
    symbol       TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    published_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    revision     INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY(symbol, period_end, published_at, revision)
);
CREATE INDEX IF NOT EXISTS idx_fundamental_pit
    ON fundamental_snapshot(symbol, available_at);

CREATE TABLE IF NOT EXISTS corporate_action (
    symbol       TEXT NOT NULL,
    action_type  TEXT NOT NULL,
    ex_date      TEXT NOT NULL,
    announced_at TEXT,
    available_at TEXT NOT NULL,
    revision     INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(symbol, action_type, ex_date, revision)
);
CREATE INDEX IF NOT EXISTS idx_corporate_action_pit
    ON corporate_action(symbol, available_at);

CREATE TABLE IF NOT EXISTS trading_calendar (
    market        TEXT NOT NULL,
    trade_date    TEXT NOT NULL,
    is_open       INTEGER NOT NULL,
    session_open  TEXT,
    session_close TEXT,
    source        TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY(market, trade_date)
);

CREATE TABLE IF NOT EXISTS api_quota_usage (
    scope          TEXT NOT NULL,
    window_start   TEXT NOT NULL,
    window_seconds INTEGER NOT NULL,
    used           INTEGER NOT NULL DEFAULT 0,
    quota_limit    INTEGER NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY(scope, window_start)
);

CREATE TABLE IF NOT EXISTS data_quality_event (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT,
    dataset      TEXT NOT NULL,
    severity     TEXT NOT NULL,
    rule_name    TEXT NOT NULL,
    details_json TEXT,
    detected_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_data_quality_symbol
    ON data_quality_event(symbol, detected_at);

-- 横截面模型只输出评分；评分与 PIT universe 快照强绑定。
CREATE TABLE IF NOT EXISTS alpha_score (
    model_id      TEXT NOT NULL,
    as_of         TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    score         REAL NOT NULL,
    rank_no       INTEGER NOT NULL,
    snapshot_id   TEXT NOT NULL REFERENCES universe_snapshot(snapshot_id),
    features_json TEXT,
    created_at    TEXT NOT NULL,
    PRIMARY KEY(model_id, as_of, symbol)
);
CREATE INDEX IF NOT EXISTS idx_alpha_score_rank
    ON alpha_score(model_id, as_of, rank_no);

-- v5 研究方案与一次性 Holdout。Holdout 按数据版本+区间唯一；结果暴露后不可
-- 再为修改后的 candidate 提供最终 OOS 资格。
CREATE TABLE IF NOT EXISTS research_holdout (
    holdout_id              TEXT PRIMARY KEY,
    symbol                  TEXT NOT NULL,
    data_version            TEXT NOT NULL,
    holdout_start           TEXT NOT NULL,
    holdout_end             TEXT NOT NULL,
    status                  TEXT NOT NULL, -- SEALED|OPENED|CONSUMED|CONTAMINATED
    candidate_version_hash  TEXT NOT NULL,
    opened_at               TEXT,
    exposure_count          INTEGER NOT NULL DEFAULT 0,
    result_json             TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    UNIQUE(symbol, data_version, holdout_start, holdout_end)
);

CREATE TABLE IF NOT EXISTS research_manifest (
    manifest_id             TEXT PRIMARY KEY,
    symbol                  TEXT NOT NULL,
    data_version            TEXT NOT NULL,
    search_space_hash       TEXT NOT NULL,
    candidate_version_hash  TEXT NOT NULL,
    holdout_id              TEXT NOT NULL REFERENCES research_holdout(holdout_id),
    payload_json            TEXT NOT NULL,
    created_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_manifest_symbol
    ON research_manifest(symbol, created_at);

-- v5 业务信号与 Transactional Outbox。两者必须在同一事务中创建。
CREATE TABLE IF NOT EXISTS signal_event (
    event_id             TEXT PRIMARY KEY,
    account_id           TEXT NOT NULL,
    symbol               TEXT NOT NULL,
    strategy_version_id  INTEGER NOT NULL,
    bar_ts               TEXT NOT NULL,
    signal_type          TEXT NOT NULL,
    payload_json         TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    UNIQUE(account_id, symbol, strategy_version_id, bar_ts, signal_type)
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    outbox_id      TEXT PRIMARY KEY,
    event_id       TEXT NOT NULL REFERENCES signal_event(event_id) ON DELETE CASCADE,
    channel        TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'PENDING',
    attempts       INTEGER NOT NULL DEFAULT 0,
    next_retry_at  TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE(event_id, channel)
);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_pending
    ON notification_outbox(status, next_retry_at, created_at);

CREATE TABLE IF NOT EXISTS monitor_alert_dedupe (
    symbol       TEXT NOT NULL,
    condition    TEXT NOT NULL,
    alert_date   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY(symbol, condition, alert_date)
);

CREATE TABLE IF NOT EXISTS system_readiness (
    gate          TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    accepted_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_canary (
    canary_id            TEXT PRIMARY KEY,
    account_id           TEXT NOT NULL,
    symbols_json         TEXT NOT NULL,
    side                 TEXT NOT NULL,
    max_notional         REAL NOT NULL,
    max_orders           INTEGER NOT NULL,
    expires_at           TEXT NOT NULL,
    status               TEXT NOT NULL,
    used_notional        REAL NOT NULL DEFAULT 0,
    used_orders          INTEGER NOT NULL DEFAULT 0,
    close_reason         TEXT,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_canary_usage (
    canary_id         TEXT NOT NULL,
    client_request_id TEXT NOT NULL UNIQUE,
    plan_id           TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    notional          REAL NOT NULL,
    created_at        TEXT NOT NULL,
    PRIMARY KEY(canary_id, client_request_id)
);

CREATE TABLE IF NOT EXISTS security_master (
    symbol       TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    exchange     TEXT NOT NULL,
    currency     TEXT NOT NULL,
    sector       TEXT NOT NULL DEFAULT 'UNKNOWN',
    asset_type   TEXT NOT NULL DEFAULT 'EQUITY',
    beta         REAL NOT NULL DEFAULT 1.0,
    leverage     REAL NOT NULL DEFAULT 1.0,
    lot_size     INTEGER,
    aliases_json TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'ACTIVE',
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist_item (
    account_id TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    reason     TEXT,
    status     TEXT NOT NULL DEFAULT 'FOLLOWING',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, symbol)
);

CREATE TABLE IF NOT EXISTS monitoring_subscription (
    account_id TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    channel    TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    last_status TEXT,
    PRIMARY KEY(account_id, symbol, channel)
);

CREATE TABLE IF NOT EXISTS strategy_assignment (
    account_id          TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    strategy_version_id INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'SUGGESTED',
    approved_by         TEXT,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY(account_id, symbol)
);

CREATE TABLE IF NOT EXISTS investor_policy (
    policy_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        TEXT NOT NULL,
    config_json       TEXT NOT NULL,
    config_hash       TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_investor_policy_hash
    ON investor_policy(account_id, config_hash);
"""

SCHEMA_VERSION = 15


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """返回表是否已有字段；表名只来自本模块的固定 migration 定义。"""
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """把任意已初始化的 v4/v5 数据库升级到当前 schema。

    SQLite 的 ``CREATE TABLE IF NOT EXISTS`` 不会给旧表补字段，因此字段迁移必须
    显式执行。每步均可重入，适合 CLI/cron 多次打开同一数据库。
    """
    if not _has_column(conn, "trading_account", "nav"):
        conn.execute("ALTER TABLE trading_account ADD COLUMN nav REAL")
    if not _has_column(conn, "strategy_version", "oos_stats_json"):
        conn.execute("ALTER TABLE strategy_version ADD COLUMN oos_stats_json TEXT")
    if not _has_column(conn, "trading_order_intent", "strategy_version_id"):
        conn.execute("ALTER TABLE trading_order_intent ADD COLUMN strategy_version_id INTEGER")
    if not _has_column(conn, "trading_order_intent", "confirmation_id"):
        conn.execute("ALTER TABLE trading_order_intent ADD COLUMN confirmation_id TEXT")
    if not _has_column(conn, "data_manifest", "adjustment_mode"):
        conn.execute("ALTER TABLE data_manifest ADD COLUMN adjustment_mode TEXT NOT NULL DEFAULT 'UNKNOWN'")
    if not _has_column(conn, "data_manifest", "corporate_actions_status"):
        conn.execute("ALTER TABLE data_manifest ADD COLUMN corporate_actions_status TEXT NOT NULL DEFAULT 'UNKNOWN'")
    if not _has_column(conn, "monitoring_subscription", "last_run_at"):
        conn.execute("ALTER TABLE monitoring_subscription ADD COLUMN last_run_at TEXT")
    if not _has_column(conn, "monitoring_subscription", "last_status"):
        conn.execute("ALTER TABLE monitoring_subscription ADD COLUMN last_status TEXT")
    if not _has_column(conn, "security_master", "sector"):
        conn.execute("ALTER TABLE security_master ADD COLUMN sector TEXT NOT NULL DEFAULT 'UNKNOWN'")
    if not _has_column(conn, "security_master", "asset_type"):
        conn.execute("ALTER TABLE security_master ADD COLUMN asset_type TEXT NOT NULL DEFAULT 'EQUITY'")
    if not _has_column(conn, "security_master", "beta"):
        conn.execute("ALTER TABLE security_master ADD COLUMN beta REAL NOT NULL DEFAULT 1.0")
    if not _has_column(conn, "security_master", "leverage"):
        conn.execute("ALTER TABLE security_master ADD COLUMN leverage REAL NOT NULL DEFAULT 1.0")
    if not _has_column(conn, "security_master", "lot_size"):
        conn.execute("ALTER TABLE security_master ADD COLUMN lot_size INTEGER")
    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, _now()),
    )
    conn.commit()


def get_conn(db_path: str = DB_PATH) -> sqlite3.Connection:
    """打开连接并初始化 schema（StateRepository 唯一入口，D-13）。

    SQLite 纪律：WAL / foreign_keys=ON / busy_timeout=5000 / 每连接独立。

    每次连接先执行幂等 DDL，再执行可重入字段 migration。这样旧数据库即使已有
    schema_version，也不会错过后续新增的表或字段。
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    # 始终执行幂等 DDL，确保旧库能拿到后来新增的表；随后显式迁移旧表字段。
    conn.executescript(SCHEMA)
    conn.executescript(SCHEMA_V4)
    _apply_migrations(conn)
    return conn


def get_core_conn(db_path: str = CORE_DB_PATH) -> sqlite3.Connection:
    """打开 Core store；新代码应优先使用这个语义明确的入口。"""
    return get_conn(db_path)


def get_execution_conn(db_path: str = EXECUTION_DB_PATH) -> sqlite3.Connection:
    """打开 executiond 私有 store。

    该函数只供 execution 包/独立 executiond 进程使用。生产部署仍必须用不同
    OS 用户或数据库 role 限制 Core/Agent 对此文件（schema）的访问；应用层检查
    不能替代操作系统权限。
    """
    return get_conn(db_path)


def get_readonly_conn(db_path: str) -> sqlite3.Connection:
    """以 SQLite mode=ro 打开 store，不执行 DDL/migration。"""
    path = os.path.abspath(db_path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def database_identity(conn: sqlite3.Connection) -> str:
    """返回 SQLite 主库的规范路径；内存库使用连接对象身份区分。"""
    row = conn.execute("PRAGMA database_list").fetchone()
    path = str(row[2] or "") if row is not None else ""
    return os.path.realpath(path) if path else f":memory:{id(conn)}"


def assert_separate_stores(core_conn: sqlite3.Connection,
                           execution_conn: sqlite3.Connection) -> None:
    """LIVE 安全边界：Core 与 executiond 不得指向同一物理数据库。"""
    if core_conn is execution_conn or database_identity(core_conn) == database_identity(execution_conn):
        raise RuntimeError("LIVE 要求 core store 与 execution store 物理隔离")


@contextmanager
def immediate_transaction(conn: sqlite3.Connection):
    """StateRepository 管理的 ``BEGIN IMMEDIATE`` 短事务。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def backup(dest_path: Optional[str] = None, db_path: str = DB_PATH) -> str:
    """在线一致性备份（D-1 备份纪律：WAL 安全）。

    用 sqlite3 Backup API（src.backup(dst)）把源库复制到目标文件：
    - 在线执行：备份期间其他连接可继续读写，无需停库
    - WAL 安全：Backup API 会把 WAL 中尚未 checkpoint 的页一并复制，
      不丢最近未落盘事务
    - 禁止直接 cp trading.db：WAL/shm 未 checkpoint 会丢数据（架构 D-1）

    返回备份文件的绝对路径。dest_path 缺省时写到
    <db 同目录>/backups/<YYYY-MM-DD>.db。
    """
    source_path = os.path.abspath(db_path)
    if not os.path.isfile(source_path):
        raise FileNotFoundError(f"备份源数据库不存在: {source_path}")
    if dest_path is None:
        from datetime import datetime, timezone
        default_name = datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".db"
        dest_path = str(Path(source_path).parent / "backups" / default_name)
    dest_path = os.path.abspath(dest_path)
    if dest_path == source_path:
        raise ValueError("备份目标不能与源数据库相同")
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)

    src = sqlite3.connect(source_path, timeout=5.0)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)  # Backup API：覆盖式写入，重复备份幂等
            check = dst.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                raise RuntimeError(f"备份完整性校验失败: {check}")
        finally:
            dst.close()
    finally:
        src.close()
    return dest_path


def compute_sha256(path: str) -> str:
    """计算文件 SHA-256（用于数据溯源）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ────────────────────────────────────────────────────────────────
# bars / data_manifest
# ────────────────────────────────────────────────────────────────

def upsert_bars(conn: sqlite3.Connection, symbol: str, rows: List[Dict], source: str) -> int:
    """批量写入日线。rows: [{ts, open, high, low, close, volume}, ...]"""
    cur = conn.executemany(
        "INSERT OR REPLACE INTO bars (symbol, ts, open, high, low, close, volume, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(symbol, r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"], source) for r in rows],
    )
    conn.commit()
    return cur.rowcount


def get_bars(conn: sqlite3.Connection, symbol: str, start: Optional[str] = None,
             end: Optional[str] = None) -> List[sqlite3.Row]:
    """读取日线（按 ts 升序）。"""
    sql = "SELECT ts, open, high, low, close, volume FROM bars WHERE symbol = ?"
    params: List = [symbol]
    if start:
        sql += " AND ts >= ?"
        params.append(start)
    if end:
        sql += " AND ts <= ?"
        params.append(end)
    sql += " ORDER BY ts"
    return conn.execute(sql, params).fetchall()


def get_all_bars(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """读取全部 OHLCV，供横截面/PIT 研究使用。"""
    return conn.execute(
        "SELECT symbol, ts, open, high, low, close, volume "
        "FROM bars ORDER BY symbol, ts"
    ).fetchall()


def set_manifest(conn: sqlite3.Connection, symbol: str, meta: Dict) -> None:
    """写入/更新数据溯源记录。"""
    simulated = str(meta.get("source", "")).lower() in {
        "test", "e2e", "synthetic", "simulation"
    }
    adjustment_mode = meta.get("adjustment_mode", "TEST" if simulated else "UNKNOWN")
    corporate_status = meta.get(
        "corporate_actions_status", "TEST" if simulated else "UNKNOWN")
    conn.execute(
        "INSERT OR REPLACE INTO data_manifest "
        "(symbol, source, fetched_at, last_completed, date_start, date_end, bar_count, sha256, "
        " adjustment_mode, corporate_actions_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol, meta["source"], meta["fetched_at"], meta["last_completed"],
         meta["date_start"], meta["date_end"], meta["bar_count"], meta["sha256"],
         adjustment_mode, corporate_status),
    )
    conn.commit()


def get_manifest(conn: sqlite3.Connection, symbol: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM data_manifest WHERE symbol = ?", (symbol,)).fetchone()


def list_manifest_symbols(conn: sqlite3.Connection) -> List[str]:
    """已缓存（在 data_manifest 表里有溯源记录）的 symbol 列表（升序）。"""
    return [r[0] for r in conn.execute(
        "SELECT symbol FROM data_manifest ORDER BY symbol").fetchall()]


def list_manifest_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    return {row["symbol"]: int(row["bar_count"] or 0) for row in conn.execute(
        "SELECT symbol, bar_count FROM data_manifest").fetchall()}


def update_manifest_increment(conn: sqlite3.Connection, symbol: str,
                              last_completed: str, date_end: str,
                              bar_count: int, fetched_at: str,
                              sha256: str) -> None:
    conn.execute(
        "UPDATE data_manifest SET last_completed=?, date_end=?, bar_count=?, "
        "fetched_at=?, sha256=? WHERE symbol=?",
        (last_completed, date_end, bar_count, fetched_at, sha256, symbol),
    )
    conn.commit()


def set_corporate_actions_status(conn: sqlite3.Connection, symbol: str,
                                 status: str) -> None:
    """只更新既有 manifest 的公司行为核对状态。"""
    if status not in ("UNKNOWN", "PROVIDER_ADJUSTED", "SYNCED", "TEST"):
        raise ValueError(f"非法 corporate actions status: {status}")
    conn.execute(
        "UPDATE data_manifest SET corporate_actions_status=? WHERE symbol=?",
        (status, symbol),
    )
    conn.commit()


# ────────────────────────────────────────────────────────────────
# lifecycle
# ────────────────────────────────────────────────────────────────

STATUSES = ("candidate", "backtesting", "research_only", "shadow", "verified", "live",
            "degraded", "suspended", "removed")


def set_lifecycle(conn: sqlite3.Connection, symbol: str, status: str,
                  evidence_hash: Optional[str] = None, fail_count: Optional[int] = None,
                  score: Optional[float] = None, params_json: Optional[str] = None) -> None:
    """更新生命周期状态。程序唯一写入入口。

    - 相同 evidence_hash 重跑：不重复累计失败次数（fail_count 不变）
    - fail_count 只增不减，除非人工显式重置
    """
    if status not in STATUSES:
        raise ValueError(f"非法状态: {status}")
    cur = conn.execute("SELECT * FROM lifecycle WHERE symbol = ?", (symbol,))
    row = cur.fetchone()
    now = _now()

    if row is None:
        conn.execute(
            "INSERT INTO lifecycle (symbol, status, fail_count, last_evidence_hash, score, params_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (symbol, status, 0 if fail_count is None else fail_count,
             evidence_hash, score, params_json, now),
        )
    elif status == "backtesting":
        # 临时状态：只更新 status/updated_at，保留证据链
        # （last_evidence_hash/fail_count/score/params_json 不动，避免清掉证据导致
        #  相同 evidence 重跑被误判为第二次失败 — 铁律 5）
        conn.execute(
            "UPDATE lifecycle SET status = ?, updated_at = ? WHERE symbol = ?",
            (status, now, symbol),
        )
    else:
        if fail_count is None:
            # 自动推断：degraded + 新证据 = 失败一次；removed = 再失败一次
            if status == "degraded" and row["last_evidence_hash"] != evidence_hash:
                fail_count = row["fail_count"] + 1
            elif status == "removed" and row["last_evidence_hash"] != evidence_hash:
                fail_count = row["fail_count"] + 1
            else:
                fail_count = row["fail_count"]
        conn.execute(
            "UPDATE lifecycle SET status = ?, fail_count = ?, last_evidence_hash = ?, "
            "score = ?, params_json = ?, updated_at = ? WHERE symbol = ?",
            (status, fail_count, evidence_hash, score, params_json, now, symbol),
        )
    conn.commit()


def get_lifecycle(conn: sqlite3.Connection, symbol: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM lifecycle WHERE symbol = ?", (symbol,)).fetchone()


def list_lifecycle(conn: sqlite3.Connection, status: Optional[str] = None) -> List[sqlite3.Row]:
    if status:
        return conn.execute("SELECT * FROM lifecycle WHERE status = ?", (status,)).fetchall()
    return conn.execute("SELECT * FROM lifecycle ORDER BY symbol").fetchall()


def verified(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """已通过回测验证的标的 = status='verified'（旧称 active_watchlist）。"""
    return list_lifecycle(conn, "verified")


def save_strategy_version(conn, symbol: str, status: str,
                          params_json: Optional[str] = None,
                          wf_report_json: Optional[str] = None,
                          git_commit: Optional[str] = None,
                          code_hash: Optional[str] = None,
                          data_version: Optional[str] = None,
                          oos_stats_json: Optional[str] = None) -> int:
    """保存策略版本快照（R1#11 版本固化）。

    在 lifecycle 状态变更时调用，固化当时的代码版本/数据版本/参数/WF report，
    确保 "为什么买 NVDA" 可从 StrategyVersion → Signal → Plan → Confirmation 完整追溯。
    """
    conn.execute(
        "INSERT INTO strategy_version "
        "(symbol, git_commit, code_hash, data_version, params_json, wf_report_json, "
        " oos_stats_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol, git_commit, code_hash, data_version, params_json, wf_report_json,
         oos_stats_json, status, _now()),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def list_strategy_versions(conn, symbol: Optional[str] = None,
                           limit: Optional[int] = None,
                           newest_first: bool = False) -> List[sqlite3.Row]:
    """查看策略版本快照；默认保持旧接口的升序和不限条数语义。"""
    sql = "SELECT * FROM strategy_version"
    params: List[Any] = []
    if symbol:
        sql += " WHERE symbol = ?"
        params.append(symbol)
    sql += " ORDER BY version_id " + ("DESC" if newest_first else "ASC")
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def get_latest_strategy_version(conn, symbol: str) -> Optional[sqlite3.Row]:
    """读取标的最新的不可变策略版本快照。"""
    return conn.execute(
        "SELECT * FROM strategy_version WHERE symbol = ? ORDER BY version_id DESC LIMIT 1",
        (symbol,),
    ).fetchone()


# ────────────────────────────────────────────────────────────────
# portfolio
# ────────────────────────────────────────────────────────────────

def upsert_position(conn: sqlite3.Connection, symbol: str, entry_price: float, entry_ts: str,
                    quantity: float, stop_price: float, peak_high: float, batch: int = 1) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO portfolio "
        "(symbol, entry_price, entry_ts, quantity, stop_price, peak_high, batch, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol, entry_price, entry_ts, quantity, stop_price, peak_high, batch, _now()),
    )
    conn.commit()


def get_position(conn: sqlite3.Connection, symbol: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM portfolio WHERE symbol = ?", (symbol,)).fetchone()


def list_positions(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM portfolio ORDER BY symbol").fetchall()


# ────────────────────────────────────────────────────────────────
# research_archive
# ────────────────────────────────────────────────────────────────

def archive(conn: sqlite3.Connection, symbol: str, status: str, params_json: Optional[str],
            score: Optional[float], evidence_json: Optional[str]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO research_archive "
        "(symbol, status, params_json, score, evidence_json, archived_at) VALUES (?, ?, ?, ?, ?, ?)",
        (symbol, status, params_json, score, evidence_json, _now()),
    )
    conn.commit()


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ────────────────────────────────────────────────────────────────
# v4.0 audit 层（D-10 lineage）
# ────────────────────────────────────────────────────────────────

def audit(conn: sqlite3.Connection, event: str, entity_type: Optional[str] = None,
          entity_id: Optional[str] = None, payload: Optional[Dict] = None,
          commit: bool = True) -> None:
    """写一条 audit lineage 事件。所有关键状态变更都应留痕。"""
    conn.execute(
        "INSERT INTO audit_log (ts, event, entity_type, entity_id, payload_json) VALUES (?, ?, ?, ?, ?)",
        (_now(), event, entity_type, entity_id,
         json.dumps(payload, ensure_ascii=False) if payload is not None else None),
    )
    if commit:
        conn.commit()


def get_audit(conn: sqlite3.Connection, entity_type: Optional[str] = None,
              entity_id: Optional[str] = None, limit: int = 100) -> List[sqlite3.Row]:
    sql = "SELECT * FROM audit_log"
    params: List = []
    if entity_type:
        sql += " WHERE entity_type = ?"
        params.append(entity_type)
    if entity_id:
        sql += (" AND" if entity_type else " WHERE") + " entity_id = ?"
        params.append(entity_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


# ────────────────────────────────────────────────────────────────
# v5 ResearchManifest / 一次性 Holdout
# ────────────────────────────────────────────────────────────────

def seal_research_holdout(conn: sqlite3.Connection, holdout_id: str, symbol: str,
                          data_version: str, holdout_start: str, holdout_end: str,
                          candidate_version_hash: str) -> sqlite3.Row:
    """封存 Holdout；同一区间被不同 candidate 复用时立即标记污染。"""
    with immediate_transaction(conn):
        row = conn.execute(
            "SELECT * FROM research_holdout WHERE holdout_id = ?", (holdout_id,),
        ).fetchone()
        if row is None:
            now = _now()
            conn.execute(
                "INSERT INTO research_holdout "
                "(holdout_id, symbol, data_version, holdout_start, holdout_end, status, "
                " candidate_version_hash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'SEALED', ?, ?, ?)",
                (holdout_id, symbol, data_version, holdout_start, holdout_end,
                 candidate_version_hash, now, now),
            )
            audit(conn, "HOLDOUT_SEALED", "holdout", holdout_id,
                  {"symbol": symbol, "start": holdout_start, "end": holdout_end,
                   "candidate_version_hash": candidate_version_hash}, commit=False)
        elif row["candidate_version_hash"] != candidate_version_hash:
            conn.execute(
                "UPDATE research_holdout SET status='CONTAMINATED', updated_at=? "
                "WHERE holdout_id=?", (_now(), holdout_id),
            )
            audit(conn, "HOLDOUT_CONTAMINATED", "holdout", holdout_id,
                  {"reason": "candidate_changed", "previous": row["candidate_version_hash"],
                   "attempted": candidate_version_hash}, commit=False)
    return conn.execute(
        "SELECT * FROM research_holdout WHERE holdout_id = ?", (holdout_id,),
    ).fetchone()


def open_research_holdout(conn: sqlite3.Connection, holdout_id: str,
                          candidate_version_hash: str) -> Dict[str, Any]:
    """打开唯一一次最终评估；幂等重放返回缓存，其他复用一律污染。"""
    outcome = "OPENED"
    with immediate_transaction(conn):
        row = conn.execute(
            "SELECT * FROM research_holdout WHERE holdout_id = ?", (holdout_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"holdout 未封存: {holdout_id}")
        same_candidate = row["candidate_version_hash"] == candidate_version_hash
        if row["status"] == "SEALED" and same_candidate:
            conn.execute(
                "UPDATE research_holdout SET status='OPENED', opened_at=?, "
                "exposure_count=exposure_count+1, updated_at=? WHERE holdout_id=?",
                (_now(), _now(), holdout_id),
            )
            audit(conn, "HOLDOUT_OPENED", "holdout", holdout_id,
                  {"candidate_version_hash": candidate_version_hash}, commit=False)
        elif row["status"] == "CONSUMED" and same_candidate:
            outcome = "CACHED"
        else:
            outcome = "CONTAMINATED"
            conn.execute(
                "UPDATE research_holdout SET status='CONTAMINATED', "
                "exposure_count=exposure_count+1, updated_at=? WHERE holdout_id=?",
                (_now(), holdout_id),
            )
            audit(conn, "HOLDOUT_CONTAMINATED", "holdout", holdout_id,
                  {"reason": f"reopen_{row['status'].lower()}",
                   "candidate_version_hash": candidate_version_hash}, commit=False)
    current = conn.execute(
        "SELECT * FROM research_holdout WHERE holdout_id = ?", (holdout_id,),
    ).fetchone()
    return {"outcome": outcome, "row": current,
            "result": json.loads(current["result_json"])
            if current["result_json"] else None}


def consume_research_holdout(conn: sqlite3.Connection, holdout_id: str,
                             candidate_version_hash: str,
                             result: Dict[str, Any]) -> sqlite3.Row:
    """首次结果暴露即消费；非 OPENED 或 candidate 不匹配时拒绝。"""
    with immediate_transaction(conn):
        row = conn.execute(
            "SELECT * FROM research_holdout WHERE holdout_id = ?", (holdout_id,),
        ).fetchone()
        if (row is None or row["status"] != "OPENED"
                or row["candidate_version_hash"] != candidate_version_hash):
            raise RuntimeError("Holdout 非 OPENED 或 candidate hash 不匹配")
        conn.execute(
            "UPDATE research_holdout SET status='CONSUMED', result_json=?, updated_at=? "
            "WHERE holdout_id=?",
            (json.dumps(result, ensure_ascii=False, sort_keys=True), _now(), holdout_id),
        )
        audit(conn, "HOLDOUT_CONSUMED", "holdout", holdout_id,
              {"candidate_version_hash": candidate_version_hash,
               "passed": bool(result.get("passed"))}, commit=False)
    return get_research_holdout(conn, holdout_id)


def get_research_holdout(conn: sqlite3.Connection,
                         holdout_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM research_holdout WHERE holdout_id = ?", (holdout_id,),
    ).fetchone()


def save_research_manifest(conn: sqlite3.Connection, manifest_id: str, symbol: str,
                           data_version: str, search_space_hash: str,
                           candidate_version_hash: str, holdout_id: str,
                           payload: Dict[str, Any]) -> None:
    """保存不可变研究方案；同 id 只允许内容完全相同的幂等重放。"""
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    existing = conn.execute(
        "SELECT * FROM research_manifest WHERE manifest_id = ?", (manifest_id,),
    ).fetchone()
    if existing is not None:
        if (existing["payload_json"] != payload_json
                or existing["candidate_version_hash"] != candidate_version_hash
                or existing["holdout_id"] != holdout_id):
            raise ValueError(f"research manifest 不可覆盖: {manifest_id}")
        return
    conn.execute(
        "INSERT INTO research_manifest "
        "(manifest_id, symbol, data_version, search_space_hash, "
        " candidate_version_hash, holdout_id, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (manifest_id, symbol, data_version, search_space_hash,
         candidate_version_hash, holdout_id, payload_json, _now()),
    )
    conn.commit()


def get_research_manifest(conn: sqlite3.Connection,
                          manifest_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM research_manifest WHERE manifest_id = ?", (manifest_id,),
    ).fetchone()


# ────────────────────────────────────────────────────────────────
# v5 SignalEvent + Transactional Notification Outbox
# ────────────────────────────────────────────────────────────────

def _event_hash(prefix: str, payload: Dict[str, Any], length: int = 24) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"{prefix}_{digest[:length]}"


def record_signal_with_outbox(
    conn: sqlite3.Connection, *, account_id: str, symbol: str,
    strategy_version_id: int, bar_ts: str, signal_type: str,
    payload: Dict[str, Any], channels: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """原子创建 SignalEvent 与每渠道 Outbox；重复运行只返回既有记录。"""
    channels = channels or ["configured"]
    key = {
        "account_id": account_id, "symbol": symbol,
        "strategy_version_id": int(strategy_version_id), "bar_ts": bar_ts,
        "signal_type": signal_type,
    }
    event_id = _event_hash("sig", key)
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    created = False
    with immediate_transaction(conn):
        cursor = conn.execute(
            "INSERT OR IGNORE INTO signal_event "
            "(event_id, account_id, symbol, strategy_version_id, bar_ts, signal_type, "
            " payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, account_id, symbol, int(strategy_version_id), bar_ts,
             signal_type, payload_json, _now()),
        )
        created = cursor.rowcount == 1
        existing = conn.execute(
            "SELECT payload_json FROM signal_event WHERE event_id=?", (event_id,),
        ).fetchone()
        if existing is None:
            raise RuntimeError("SignalEvent 插入后不可见")
        # 同一业务键是不可变事实；重放内容不同说明上游不确定，必须 fail closed。
        if existing["payload_json"] != payload_json:
            raise RuntimeError(f"SignalEvent immutable payload mismatch: {event_id}")
        for channel in sorted(set(channels)):
            outbox_id = _event_hash(
                "out", {"event_id": event_id, "channel": channel})
            conn.execute(
                "INSERT OR IGNORE INTO notification_outbox "
                "(outbox_id, event_id, channel, payload_json, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'PENDING', ?, ?)",
                (outbox_id, event_id, channel, payload_json, _now(), _now()),
            )
        audit(conn, "SIGNAL_EVENT", "signal", event_id,
              {**key, "created": created, "channels": channels}, commit=False)
    return {
        "event": conn.execute(
            "SELECT * FROM signal_event WHERE event_id=?", (event_id,),
        ).fetchone(),
        "outbox": conn.execute(
            "SELECT * FROM notification_outbox WHERE event_id=? ORDER BY channel",
            (event_id,),
        ).fetchall(),
        "created": created,
    }


def list_notification_outbox(conn: sqlite3.Connection,
                             status: Optional[str] = "PENDING",
                             limit: int = 100) -> List[sqlite3.Row]:
    if status is None:
        return conn.execute(
            "SELECT * FROM notification_outbox ORDER BY created_at LIMIT ?", (limit,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM notification_outbox WHERE status=? "
        "ORDER BY created_at LIMIT ?", (status, limit),
    ).fetchall()


def mark_notification_outbox(conn: sqlite3.Connection, outbox_id: str,
                             status: str, next_retry_at: Optional[str] = None) -> None:
    if status not in ("SENDING", "SENT", "FAILED_RETRYABLE", "DEAD_LETTER"):
        raise ValueError(f"非法 outbox status: {status}")
    conn.execute(
        "UPDATE notification_outbox SET status=?, "
        "attempts=attempts + CASE WHEN ?='SENDING' THEN 1 ELSE 0 END, "
        "next_retry_at=?, updated_at=? WHERE outbox_id=?",
        (status, status, next_retry_at, _now(), outbox_id),
    )
    conn.commit()


def claim_monitor_alert(conn: sqlite3.Connection, symbol: str,
                        condition: str, alert_date: str) -> bool:
    """持久化日内提醒去重；跨进程/重启只有首次 claim 返回 True。"""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO monitor_alert_dedupe "
        "(symbol, condition, alert_date, created_at) VALUES (?, ?, ?, ?)",
        (symbol, condition, alert_date, _now()),
    )
    conn.commit()
    return cursor.rowcount == 1


# ────────────────────────────────────────────────────────────────
# v5 Manual Live Canary
# ────────────────────────────────────────────────────────────────

def mark_system_readiness(conn: sqlite3.Connection, gate: str,
                          evidence_hash: str, status: str = "PASS") -> None:
    """写入自动验收门结果；LIVE_CANARY 只承认 P0_A=PASS。"""
    if gate not in ("P0_A", "P0_B") or status not in ("PASS", "FAIL"):
        raise ValueError("非法 readiness gate/status")
    conn.execute(
        "INSERT OR REPLACE INTO system_readiness(gate,status,evidence_hash,accepted_at) "
        "VALUES(?,?,?,?)", (gate, status, evidence_hash, _now()))
    conn.commit()


def create_live_canary(conn: sqlite3.Connection, account_id: str,
                       symbols: List[str], side: str, max_notional: float,
                       max_orders: int, expires_at: str,
                       canary_id: Optional[str] = None) -> sqlite3.Row:
    """创建用户显式、强限额的 LIVE_CANARY 解锁记录。"""
    ready = conn.execute(
        "SELECT * FROM system_readiness WHERE gate='P0_A' AND status='PASS'"
    ).fetchone()
    if ready is None:
        raise RuntimeError("P0-A 尚未通过，禁止开启 LIVE_CANARY")
    if not account_id or not symbols or side not in ("BUY", "SELL"):
        raise ValueError("Canary 必须限定 account/symbol/side")
    if max_notional <= 0 or max_orders <= 0 or expires_at <= _now():
        raise ValueError("Canary 额度、订单数和有效期必须有效")
    cid = canary_id or f"canary_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO live_canary(canary_id,account_id,symbols_json,side,max_notional,"
        "max_orders,expires_at,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (cid, account_id, json.dumps(sorted(set(symbols))), side,
         float(max_notional), int(max_orders), expires_at, "ACTIVE", _now()),
    )
    conn.commit()
    audit(conn, "LIVE_CANARY_OPENED", "live_canary", cid,
          {"account_id": account_id, "symbols": sorted(set(symbols)), "side": side,
           "max_notional": max_notional, "max_orders": max_orders,
           "expires_at": expires_at, "p0_a_evidence": ready["evidence_hash"]})
    return conn.execute("SELECT * FROM live_canary WHERE canary_id=?", (cid,)).fetchone()


def authorize_live_canary(conn: sqlite3.Connection, *, account_id: str,
                          plan_id: str, client_request_id: str, symbol: str,
                          side: str, quantity: float,
                          reference_price: Optional[float]) -> sqlite3.Row:
    """原子预留 Canary 额度；同 client_request_id 重试幂等。"""
    if reference_price is None or reference_price <= 0 or quantity <= 0:
        raise RuntimeError("LIVE_CANARY 无法计算名义金额")
    notional = float(quantity) * float(reference_price)
    with immediate_transaction(conn):
        existing = conn.execute(
            "SELECT c.* FROM live_canary_usage u JOIN live_canary c USING(canary_id) "
            "WHERE u.client_request_id=?", (client_request_id,),
        ).fetchone()
        if existing is not None:
            return existing
        rows = conn.execute(
            "SELECT * FROM live_canary WHERE account_id=? AND side=? AND status='ACTIVE' "
            "AND expires_at>=? ORDER BY created_at", (account_id, side, _now()),
        ).fetchall()
        selected = None
        for row in rows:
            if symbol not in json.loads(row["symbols_json"]):
                continue
            if row["used_orders"] + 1 > row["max_orders"]:
                continue
            if row["used_notional"] + notional > row["max_notional"] + 1e-9:
                continue
            selected = row
            break
        if selected is None:
            raise RuntimeError("无匹配或额度充足的 ACTIVE LIVE_CANARY")
        conn.execute(
            "INSERT INTO live_canary_usage(canary_id,client_request_id,plan_id,symbol,"
            "side,notional,created_at) VALUES(?,?,?,?,?,?,?)",
            (selected["canary_id"], client_request_id, plan_id, symbol, side,
             notional, _now()),
        )
        conn.execute(
            "UPDATE live_canary SET used_orders=used_orders+1, "
            "used_notional=used_notional+? WHERE canary_id=?",
            (notional, selected["canary_id"]),
        )
        audit(conn, "LIVE_CANARY_AUTHORIZED", "plan", plan_id,
              {"canary_id": selected["canary_id"], "client_request_id": client_request_id,
               "notional": notional}, commit=False)
    return conn.execute(
        "SELECT * FROM live_canary WHERE canary_id=?", (selected["canary_id"],)
    ).fetchone()


def close_live_canaries(conn: sqlite3.Connection, reason: str,
                        account_id: Optional[str] = None) -> int:
    """UNKNOWN/MISMATCH/credential error 时立即关闭 Canary。"""
    if account_id:
        changed = conn.execute(
            "UPDATE live_canary SET status='CLOSED',close_reason=? "
            "WHERE status='ACTIVE' AND account_id=?", (reason, account_id)).rowcount
    else:
        changed = conn.execute(
            "UPDATE live_canary SET status='CLOSED',close_reason=? WHERE status='ACTIVE'",
            (reason,)).rowcount
    conn.commit()
    if changed:
        audit(conn, "LIVE_CANARY_CLOSED", "account", account_id or "*",
              {"reason": reason, "count": changed})
    return changed


# ────────────────────────────────────────────────────────────────
# v5 Security Master / Watchlist / InvestorPolicy
# ────────────────────────────────────────────────────────────────

def upsert_security(conn: sqlite3.Connection, symbol: str, name: str,
                    exchange: str, currency: str,
                    aliases: Optional[List[str]] = None, *, sector: str = "UNKNOWN",
                    asset_type: str = "EQUITY", beta: float = 1.0,
                    leverage: float = 1.0, lot_size: Optional[int] = None) -> None:
    conn.execute(
        "INSERT INTO security_master(symbol,name,exchange,currency,sector,asset_type,beta,"
        "leverage,lot_size,aliases_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name,"
        "exchange=excluded.exchange,currency=excluded.currency,"
        "sector=excluded.sector,asset_type=excluded.asset_type,beta=excluded.beta,"
        "leverage=excluded.leverage,lot_size=excluded.lot_size,"
        "aliases_json=excluded.aliases_json,updated_at=excluded.updated_at",
        (symbol.upper(), name, exchange.upper(), currency.upper(), sector.upper(),
         asset_type.upper(), float(beta), float(leverage),
         int(lot_size) if lot_size is not None else None,
         json.dumps(sorted(set(aliases or [])), ensure_ascii=False), _now()),
    )
    conn.commit()


def get_security(conn: sqlite3.Connection, symbol: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM security_master WHERE symbol=?", (symbol.upper(),)).fetchone()


def search_security(conn: sqlite3.Connection, query: str) -> List[Dict]:
    q = query.strip().casefold()
    matches = []
    for row in conn.execute("SELECT * FROM security_master WHERE status='ACTIVE'"):
        aliases = json.loads(row["aliases_json"])
        exact_symbol = q == row["symbol"].casefold()
        exact_name = q == row["name"].casefold() or any(q == a.casefold() for a in aliases)
        partial = (q in row["symbol"].casefold() or q in row["name"].casefold()
                   or any(q in a.casefold() for a in aliases))
        if exact_symbol or exact_name or partial:
            matches.append({**dict(row), "aliases": aliases,
                            "confidence": 1.0 if exact_symbol else 0.95 if exact_name else 0.6})
    return sorted(matches, key=lambda item: (-item["confidence"], item["symbol"]))


def follow_security(conn: sqlite3.Connection, account_id: str, symbol: str,
                    reason: str = "", channels: Optional[List[str]] = None) -> Dict:
    now = _now()
    conn.execute(
        "INSERT INTO watchlist_item(account_id,symbol,reason,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(account_id,symbol) DO UPDATE SET "
        "reason=excluded.reason,status='FOLLOWING',updated_at=excluded.updated_at",
        (account_id, symbol, reason, "FOLLOWING", now, now),
    )
    for channel in channels or ["audit"]:
        conn.execute(
            "INSERT INTO monitoring_subscription(account_id,symbol,channel,enabled,created_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(account_id,symbol,channel) DO UPDATE SET enabled=1",
            (account_id, symbol, channel, 1, now),
        )
    conn.commit()
    audit(conn, "SECURITY_FOLLOWED", "symbol", symbol,
          {"account_id": account_id, "reason": reason, "channels": channels or ["audit"]})
    return dict(conn.execute(
        "SELECT * FROM watchlist_item WHERE account_id=? AND symbol=?",
        (account_id, symbol)).fetchone())


DEFAULT_INVESTOR_POLICY = {
    "risk_per_trade": 0.005,
    "max_single_position": 0.10,
    "max_gross_notional": 0.25,
    "max_stop_risk": 0.015,
    "max_group_exposure": 0.15,
    "max_pair_exposure": 0.15,
    "max_sector_exposure": 0.15,
    "max_currency_exposure": 0.25,
    "max_beta_weighted_exposure": 0.35,
    "max_event_risk_exposure": 0.10,
    "max_adv_participation": 0.05,
    "allowed_asset_types": ["EQUITY", "ETF"],
    "allowed_currencies": ["USD", "HKD", "CNY"],
}


def save_investor_policy(conn: sqlite3.Connection, account_id: str,
                         config: Dict) -> sqlite3.Row:
    merged = {**DEFAULT_INVESTOR_POLICY, **dict(config)}
    canonical = json.dumps(merged, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    existing = conn.execute(
        "SELECT * FROM investor_policy WHERE account_id=? AND config_hash=?",
        (account_id, digest)).fetchone()
    if existing is not None:
        return existing
    with immediate_transaction(conn):
        conn.execute("UPDATE investor_policy SET status='SUPERSEDED' "
                     "WHERE account_id=? AND status='ACTIVE'", (account_id,))
        conn.execute(
            "INSERT INTO investor_policy(account_id,config_json,config_hash,status,created_at) "
            "VALUES(?,?,?,?,?)", (account_id, canonical, digest, "ACTIVE", _now()))
    return conn.execute(
        "SELECT * FROM investor_policy WHERE account_id=? AND config_hash=?",
        (account_id, digest)).fetchone()


def get_active_investor_policy(conn: sqlite3.Connection,
                               account_id: str = "default") -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM investor_policy WHERE account_id=? AND status='ACTIVE' "
        "ORDER BY policy_version_id DESC LIMIT 1", (account_id,)).fetchone()
    return row if row is not None else save_investor_policy(
        conn, account_id, DEFAULT_INVESTOR_POLICY)


# ────────────────────────────────────────────────────────────────
# v4.0 UniverseSnapshot（D-5）
# ────────────────────────────────────────────────────────────────

def snapshot_universe(conn: sqlite3.Connection, source: str,
                      symbols: List[Any], status: str = "active",
                      as_of_date: Optional[str] = None,
                      metadata: Optional[Dict] = None) -> str:
    """保存不可变的历史 UniverseSnapshot，并维护旧 latest 表兼容视图。

    ``symbols`` 可为代码字符串，也可为带 exchange/sector/listed_at/delisted_at
    的字典。同一 source + as_of_date 重放只有内容完全一致时才幂等返回；内容变化
    会拒绝覆盖，避免幸存者偏差数据被静默改写。
    """
    captured_at = _now()
    snapshot_date = as_of_date or captured_at[:10]
    normalized = []
    for item in symbols:
        member = {"symbol": item} if isinstance(item, str) else dict(item)
        if not member.get("symbol"):
            raise ValueError("Universe member 缺少 symbol")
        member.setdefault("status", status)
        normalized.append(member)
    normalized.sort(key=lambda item: item["symbol"])

    existing = conn.execute(
        "SELECT snapshot_id FROM universe_snapshot WHERE source=? AND as_of_date=?",
        (source, snapshot_date),
    ).fetchone()
    if existing is not None:
        stored = [dict(row) for row in conn.execute(
            "SELECT symbol, status, exchange, sector, listed_at, delisted_at "
            "FROM universe_member WHERE snapshot_id=? ORDER BY symbol",
            (existing["snapshot_id"],),
        ).fetchall()]
        expected = [{key: member.get(key) for key in
                     ("symbol", "status", "exchange", "sector", "listed_at", "delisted_at")}
                    for member in normalized]
        if stored != expected:
            raise ValueError(
                f"UniverseSnapshot {source}/{snapshot_date} 已存在且内容不同，禁止覆盖")
        return existing["snapshot_id"]

    snapshot_id = f"uv_{snapshot_date.replace('-', '')}_{uuid.uuid4().hex[:10]}"
    with immediate_transaction(conn):
        conn.execute(
            "INSERT INTO universe_snapshot "
            "(snapshot_id, source, as_of_date, captured_at, metadata_json) VALUES (?, ?, ?, ?, ?)",
            (snapshot_id, source, snapshot_date, captured_at,
             json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)),
        )
        for member in normalized:
            conn.execute(
                "INSERT INTO universe_member "
                "(snapshot_id, symbol, status, exchange, sector, listed_at, delisted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (snapshot_id, member["symbol"], member["status"], member.get("exchange"),
                 member.get("sector"), member.get("listed_at"), member.get("delisted_at")),
            )
            conn.execute(
                "INSERT INTO trading_universe (symbol, source, added_at, status, snapshot_ts) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET "
                "source=excluded.source, status=excluded.status, snapshot_ts=excluded.snapshot_ts",
                (member["symbol"], source, captured_at, member["status"], captured_at),
            )
    audit(conn, "UNIVERSE_SNAPSHOT", entity_type="universe_snapshot", entity_id=snapshot_id,
          payload={"source": source, "as_of_date": snapshot_date,
                   "symbols": [item["symbol"] for item in normalized]})
    return snapshot_id


def list_universe_snapshots(conn: sqlite3.Connection,
                            source: Optional[str] = None) -> List[sqlite3.Row]:
    if source:
        return conn.execute(
            "SELECT * FROM universe_snapshot WHERE source=? ORDER BY as_of_date, captured_at",
            (source,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM universe_snapshot ORDER BY source, as_of_date, captured_at"
    ).fetchall()


def universe_as_of(conn: sqlite3.Connection, as_of_date: str,
                   source: Optional[str] = None) -> List[sqlite3.Row]:
    """返回指定日期可见的最近一次历史股票池成员。"""
    if source:
        snapshot = conn.execute(
            "SELECT snapshot_id FROM universe_snapshot "
            "WHERE source=? AND as_of_date<=? ORDER BY as_of_date DESC, captured_at DESC LIMIT 1",
            (source, as_of_date),
        ).fetchone()
        if snapshot is None:
            return []
        return conn.execute(
            "SELECT * FROM universe_member WHERE snapshot_id=? ORDER BY symbol",
            (snapshot["snapshot_id"],),
        ).fetchall()
    return conn.execute(
        "WITH latest AS (SELECT source, MAX(as_of_date) AS as_of_date "
        "FROM universe_snapshot WHERE as_of_date<=? GROUP BY source) "
        "SELECT m.* FROM latest l JOIN universe_snapshot s "
        "ON s.source=l.source AND s.as_of_date=l.as_of_date "
        "JOIN universe_member m ON m.snapshot_id=s.snapshot_id ORDER BY m.symbol",
        (as_of_date,),
    ).fetchall()


def list_universe(conn: sqlite3.Connection, source: Optional[str] = None) -> List[sqlite3.Row]:
    if source:
        return conn.execute("SELECT * FROM trading_universe WHERE source = ? ORDER BY symbol", (source,)).fetchall()
    return conn.execute("SELECT * FROM trading_universe ORDER BY source, symbol").fetchall()


def get_universe(conn: sqlite3.Connection, symbol: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM trading_universe WHERE symbol = ?", (symbol,)).fetchone()


# ────────────────────────────────────────────────────────────────
# v4.0 DataHub PIT / Calendar / Quota / DataQuality
# ────────────────────────────────────────────────────────────────

def upsert_fundamental(conn: sqlite3.Connection, symbol: str, period_end: str,
                       published_at: str, available_at: str, payload: Dict,
                       source: str = "longbridge", revision: int = 0) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO fundamental_snapshot "
        "(symbol, period_end, published_at, available_at, revision, source, payload_json, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol, period_end, published_at, available_at, revision, source,
         json.dumps(payload, ensure_ascii=False, sort_keys=True), _now()),
    )
    conn.commit()


def append_fundamental_revision(conn: sqlite3.Connection, symbol: str,
                                period_end: str, published_at: str,
                                available_at: str, payload: Dict,
                                source: str = "longbridge") -> int:
    """内容不变则幂等，内容变化则追加 revision，绝不覆盖历史版本。"""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    latest = conn.execute(
        "SELECT revision, payload_json FROM fundamental_snapshot "
        "WHERE symbol=? AND period_end=? ORDER BY revision DESC LIMIT 1",
        (symbol, period_end),
    ).fetchone()
    if latest is not None and latest["payload_json"] == encoded:
        return int(latest["revision"])
    revision = int(latest["revision"]) + 1 if latest is not None else 0
    conn.execute(
        "INSERT INTO fundamental_snapshot "
        "(symbol, period_end, published_at, available_at, revision, source, payload_json, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol, period_end, published_at, available_at, revision, source,
         encoded, _now()),
    )
    conn.commit()
    return revision


def fundamentals_as_of(conn: sqlite3.Connection, symbol: str,
                       as_of: str) -> List[sqlite3.Row]:
    """PIT 查询：只返回当时已经 available 的最新 revision。"""
    cutoff = as_of if "T" in as_of else as_of + "T23:59:59Z"
    return conn.execute(
        "SELECT f.* FROM fundamental_snapshot f JOIN ("
        " SELECT period_end, MAX(revision) revision FROM fundamental_snapshot "
        " WHERE symbol=? AND available_at<=? GROUP BY period_end"
        ") latest ON latest.period_end=f.period_end AND latest.revision=f.revision "
        "WHERE f.symbol=? AND f.available_at<=? ORDER BY f.period_end",
        (symbol, cutoff, symbol, cutoff),
    ).fetchall()


def upsert_corporate_action(conn: sqlite3.Connection, symbol: str,
                            action_type: str, ex_date: str, available_at: str,
                            payload: Dict, source: str = "longbridge",
                            announced_at: Optional[str] = None,
                            revision: int = 0) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO corporate_action "
        "(symbol, action_type, ex_date, announced_at, available_at, revision, source, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol, action_type, ex_date, announced_at, available_at, revision, source,
         json.dumps(payload, ensure_ascii=False, sort_keys=True)),
    )
    conn.commit()


def append_corporate_action_revision(conn: sqlite3.Connection, symbol: str,
                                     action_type: str, ex_date: str,
                                     available_at: str, payload: Dict,
                                     source: str = "longbridge",
                                     announced_at: Optional[str] = None) -> int:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    latest = conn.execute(
        "SELECT revision, payload_json FROM corporate_action "
        "WHERE symbol=? AND action_type=? AND ex_date=? "
        "ORDER BY revision DESC LIMIT 1", (symbol, action_type, ex_date),
    ).fetchone()
    if latest is not None and latest["payload_json"] == encoded:
        return int(latest["revision"])
    revision = int(latest["revision"]) + 1 if latest is not None else 0
    conn.execute(
        "INSERT INTO corporate_action "
        "(symbol, action_type, ex_date, announced_at, available_at, revision, source, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol, action_type, ex_date, announced_at, available_at, revision,
         source, encoded),
    )
    conn.commit()
    return revision


def corporate_actions_as_of(conn: sqlite3.Connection, symbol: str,
                            as_of: str) -> List[sqlite3.Row]:
    cutoff = as_of if "T" in as_of else as_of + "T23:59:59Z"
    return conn.execute(
        "SELECT * FROM corporate_action WHERE symbol=? AND available_at<=? "
        "ORDER BY ex_date, revision", (symbol, cutoff)
    ).fetchall()


def upsert_calendar(conn: sqlite3.Connection, market: str,
                    sessions: List[Dict], source: str = "longbridge") -> int:
    conn.executemany(
        "INSERT OR REPLACE INTO trading_calendar "
        "(market, trade_date, is_open, session_open, session_close, source, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(market, item["trade_date"], int(bool(item.get("is_open", True))),
          item.get("session_open"), item.get("session_close"), source, _now())
         for item in sessions],
    )
    conn.commit()
    return len(sessions)


def calendar_between(conn: sqlite3.Connection, market: str,
                     start: str, end: str, open_only: bool = False) -> List[sqlite3.Row]:
    sql = "SELECT * FROM trading_calendar WHERE market=? AND trade_date BETWEEN ? AND ?"
    if open_only:
        sql += " AND is_open=1"
    return conn.execute(sql + " ORDER BY trade_date", (market, start, end)).fetchall()


def reserve_api_quota(conn: sqlite3.Connection, scope: str, amount: int = 1,
                      quota_limit: int = 1000, window_seconds: int = 86400,
                      at: Optional[str] = None) -> Dict:
    """原子预留 API quota；超限返回 allowed=False，不增加 used。"""
    from datetime import datetime, timezone
    now = datetime.fromisoformat((at or _now()).replace("Z", "+00:00"))
    epoch = int(now.timestamp())
    start_epoch = epoch - epoch % window_seconds
    window_start = datetime.fromtimestamp(start_epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with immediate_transaction(conn):
        row = conn.execute(
            "SELECT used, quota_limit FROM api_quota_usage WHERE scope=? AND window_start=?",
            (scope, window_start),
        ).fetchone()
        used = int(row["used"]) if row else 0
        effective_limit = int(row["quota_limit"]) if row else int(quota_limit)
        allowed = amount >= 0 and used + amount <= effective_limit
        if allowed:
            conn.execute(
                "INSERT INTO api_quota_usage "
                "(scope, window_start, window_seconds, used, quota_limit, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(scope, window_start) DO UPDATE SET "
                "used=excluded.used, quota_limit=excluded.quota_limit, updated_at=excluded.updated_at",
                (scope, window_start, window_seconds, used + amount,
                 effective_limit, _now()),
            )
    return {"allowed": allowed, "scope": scope, "used": used + amount if allowed else used,
            "limit": effective_limit, "window_start": window_start}


def record_data_quality(conn: sqlite3.Connection, dataset: str, severity: str,
                        rule_name: str, details: Optional[Dict] = None,
                        symbol: Optional[str] = None) -> int:
    conn.execute(
        "INSERT INTO data_quality_event "
        "(symbol, dataset, severity, rule_name, details_json, detected_at) VALUES (?, ?, ?, ?, ?, ?)",
        (symbol, dataset, severity, rule_name,
         json.dumps(details or {}, ensure_ascii=False, sort_keys=True), _now()),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def replace_alpha_scores(conn: sqlite3.Connection, model_id: str, as_of: str,
                         snapshot_id: str, scores: List[Dict]) -> None:
    """原子替换某模型某时点评分（研究产物，可重复生成）。"""
    with immediate_transaction(conn):
        conn.execute("DELETE FROM alpha_score WHERE model_id=? AND as_of=?",
                     (model_id, as_of))
        conn.executemany(
            "INSERT INTO alpha_score "
            "(model_id, as_of, symbol, score, rank_no, snapshot_id, features_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(model_id, as_of, item["symbol"], float(item["score"]),
              int(item["rank"]), snapshot_id,
              json.dumps(item.get("features", {}), ensure_ascii=False, sort_keys=True),
              _now()) for item in scores],
        )


def list_alpha_scores(conn: sqlite3.Connection, model_id: str,
                      as_of: str, limit: Optional[int] = None) -> List[sqlite3.Row]:
    sql = "SELECT * FROM alpha_score WHERE model_id=? AND as_of=? ORDER BY rank_no"
    params: List[Any] = [model_id, as_of]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


# ────────────────────────────────────────────────────────────────
# v4.0 AccountState（D-7，P2）
# ────────────────────────────────────────────────────────────────

ACCOUNT_SYNC_STATUSES = ("SYNCED", "STALE", "RECONCILING", "MISMATCH", "UNKNOWN")


def upsert_account(conn: sqlite3.Connection, account_id: str, sync_status: str,
                   cash: Optional[float] = None, buying_power: Optional[float] = None,
                   raw_json: Optional[str] = None, nav: Optional[float] = None) -> None:
    if sync_status not in ACCOUNT_SYNC_STATUSES:
        raise ValueError(f"非法 sync_status: {sync_status}")
    conn.execute(
        "INSERT OR REPLACE INTO trading_account "
        "(account_id, sync_status, cash, buying_power, nav, updated_at, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (account_id, sync_status, cash, buying_power, nav, _now(), raw_json),
    )
    conn.commit()


def get_account(conn: sqlite3.Connection, account_id: str = "default") -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM trading_account WHERE account_id = ?", (account_id,)).fetchone()


def set_account_sync_status(conn: sqlite3.Connection, account_id: str, sync_status: str) -> None:
    """单独更新 sync_status（保留 cash/buying_power）。行不存在则插入（upsert 语义）。"""
    if sync_status not in ACCOUNT_SYNC_STATUSES:
        raise ValueError(f"非法 sync_status: {sync_status}")
    conn.execute(
        "INSERT INTO trading_account (account_id, sync_status, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(account_id) DO UPDATE SET sync_status = excluded.sync_status, updated_at = excluded.updated_at",
        (account_id, sync_status, _now()),
    )
    conn.commit()


def set_account_updated_at(conn: sqlite3.Connection, account_id: str,
                           updated_at: str) -> None:
    conn.execute("UPDATE trading_account SET updated_at=? WHERE account_id=?",
                 (updated_at, account_id))
    conn.commit()


# ────────────────────────────────────────────────────────────────
# v4.0 ExecutionPlan / Confirmation / OrderIntent（D-3 / D-9，P3）
# ────────────────────────────────────────────────────────────────

PLAN_STATUSES = ("PENDING", "CONFIRMED", "CONSUMED", "EXPIRED", "REJECTED", "CANCELLED")
CONFIRMATION_STATUSES = ("PENDING", "APPROVED", "CONSUMED", "EXPIRED", "REJECTED", "CANCELLED")
INTENT_STATUSES = ("PENDING", "SUBMITTING", "SUBMITTED", "REJECTED", "FILLED", "CANCELLED", "UNKNOWN")


def insert_plan(conn: sqlite3.Connection, plan_id: str, account_id: str, execution_mode: str,
                expires_at: str, plan_hash: str, orders: List[Dict], status: str = "PENDING") -> None:
    if execution_mode not in ("DRY_RUN", "LIVE"):
        raise ValueError(f"非法 execution_mode: {execution_mode}")
    if status not in PLAN_STATUSES:
        raise ValueError(f"非法 plan status: {status}")
    existing = get_plan(conn, plan_id)
    if existing is not None:
        if (existing["account_id"] != account_id
                or existing["execution_mode"] != execution_mode
                or existing["expires_at"] != expires_at
                or existing["plan_hash"] != plan_hash
                or json.loads(existing["orders_json"]) != orders):
            raise ValueError(f"plan_id {plan_id} 已绑定其他不可变内容")
        return
    conn.execute(
        "INSERT INTO trading_execution_plan "
        "(plan_id, account_id, execution_mode, expires_at, plan_hash, orders_json, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (plan_id, account_id, execution_mode, expires_at, plan_hash,
         json.dumps(orders, ensure_ascii=False), status, _now()),
    )
    conn.commit()
    audit(conn, "PLAN_CREATED", entity_type="execution_plan", entity_id=plan_id,
          payload={"execution_mode": execution_mode, "plan_hash": plan_hash, "orders": orders})


def get_plan(conn: sqlite3.Connection, plan_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM trading_execution_plan WHERE plan_id = ?", (plan_id,)).fetchone()


def list_plans(conn: sqlite3.Connection, status: Optional[str] = None,
               limit: int = 100) -> List[sqlite3.Row]:
    """列出 ExecutionPlan，供运维查询与批量对账使用。"""
    if status:
        if status not in PLAN_STATUSES:
            raise ValueError(f"非法 plan status: {status}")
        return conn.execute(
            "SELECT * FROM trading_execution_plan WHERE status = ? "
            "ORDER BY created_at DESC LIMIT ?", (status, limit)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM trading_execution_plan ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()


def plan_ids_for_reconciliation(conn: sqlite3.Connection) -> List[str]:
    """返回含非终态 OrderIntent 的计划 ID。"""
    rows = conn.execute(
        "SELECT DISTINCT plan_id FROM trading_order_intent "
        "WHERE status IN ('SUBMITTING', 'SUBMITTED', 'UNKNOWN') ORDER BY plan_id"
    ).fetchall()
    return [row["plan_id"] for row in rows]


def set_plan_status(conn: sqlite3.Connection, plan_id: str, status: str) -> None:
    if status not in PLAN_STATUSES:
        raise ValueError(f"非法 plan status: {status}")
    conn.execute("UPDATE trading_execution_plan SET status = ? WHERE plan_id = ?", (status, plan_id))
    conn.commit()


def insert_confirmation(conn: sqlite3.Connection, confirmation_id: str, plan_id: str, plan_hash: str,
                        expires_at: str, approved_by: Optional[str] = None,
                        approval_channel: Optional[str] = None, approval_nonce: Optional[str] = None,
                        status: str = "PENDING") -> None:
    if status not in CONFIRMATION_STATUSES:
        raise ValueError(f"非法 confirmation status: {status}")
    existing = get_confirmation(conn, confirmation_id)
    approved_at = _now() if status == "APPROVED" else None
    if existing is None:
        conn.execute(
            "INSERT INTO trading_confirmation "
            "(confirmation_id, plan_id, plan_hash, status, approved_by, approval_channel, approval_nonce, "
            " approved_at, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (confirmation_id, plan_id, plan_hash, status, approved_by, approval_channel,
             approval_nonce, approved_at, expires_at, _now()),
        )
    else:
        if (existing["plan_id"] != plan_id or existing["plan_hash"] != plan_hash
                or existing["expires_at"] != expires_at):
            raise ValueError(f"confirmation_id {confirmation_id} 已绑定其他计划")
        if existing["status"] != "PENDING" or status not in ("APPROVED", "REJECTED"):
            raise ValueError(
                f"confirmation 状态不可迁移: {existing['status']} -> {status}")
        conn.execute(
            "UPDATE trading_confirmation SET status=?, approved_by=?, approval_channel=?, "
            "approval_nonce=?, approved_at=? WHERE confirmation_id=? AND status='PENDING'",
            (status, approved_by, approval_channel, approval_nonce, approved_at,
             confirmation_id),
        )
    conn.commit()
    audit(conn, "CONFIRMATION", entity_type="confirmation", entity_id=confirmation_id,
          payload={"plan_id": plan_id, "plan_hash": plan_hash, "status": status,
                   "approved_by": approved_by, "approval_channel": approval_channel})


def get_confirmation(conn: sqlite3.Connection, confirmation_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM trading_confirmation WHERE confirmation_id = ?",
                        (confirmation_id,)).fetchone()


def approval_nonce_exists(conn: sqlite3.Connection, nonce: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM trading_confirmation WHERE approval_nonce=?", (nonce,)
    ).fetchone() is not None


def approve_confirmation(conn: sqlite3.Connection, confirmation_id: str,
                         approved_by: str, approval_channel: str,
                         approval_nonce: str, expected_plan_id: Optional[str] = None,
                         expected_plan_hash: Optional[str] = None) -> sqlite3.Row:
    """原子校验身份字段/nonce/计划绑定并执行 PENDING→APPROVED。"""
    with immediate_transaction(conn):
        row = get_confirmation(conn, confirmation_id)
        if row is None:
            raise ValueError(f"confirmation 不存在: {confirmation_id}")
        if row["status"] != "PENDING":
            raise ValueError(f"confirmation 状态 {row['status']} 不可批准（只允许 PENDING）")
        if expected_plan_id is not None and row["plan_id"] != expected_plan_id:
            raise ValueError("confirmation.plan_id 与展示计划不匹配")
        if expected_plan_hash is not None and row["plan_hash"] != expected_plan_hash:
            raise ValueError("confirmation.plan_hash 与展示计划不匹配")
        if approval_nonce_exists(conn, approval_nonce):
            raise ValueError(f"approval_nonce 已使用: {approval_nonce}")
        changed = conn.execute(
            "UPDATE trading_confirmation SET status='APPROVED', approved_by=?, "
            "approval_channel=?, approval_nonce=?, approved_at=? "
            "WHERE confirmation_id=? AND status='PENDING'",
            (approved_by, approval_channel, approval_nonce, _now(), confirmation_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("Confirmation 并发批准失败")
        audit(conn, "CONFIRMATION_APPROVED", entity_type="confirmation",
              entity_id=confirmation_id,
              payload={"approved_by": approved_by, "channel": approval_channel,
                       "nonce": approval_nonce}, commit=False)
    result = get_confirmation(conn, confirmation_id)
    assert result is not None
    return result


def reject_confirmation(conn: sqlite3.Connection, confirmation_id: str,
                        rejected_by: str, approval_channel: str,
                        approval_nonce: str, reason: str = "") -> sqlite3.Row:
    with immediate_transaction(conn):
        row = get_confirmation(conn, confirmation_id)
        if row is None:
            raise ValueError(f"confirmation 不存在: {confirmation_id}")
        if row["status"] != "PENDING":
            raise ValueError(f"confirmation 状态 {row['status']} 不可拒绝（只允许 PENDING）")
        if approval_nonce_exists(conn, approval_nonce):
            raise ValueError(f"approval_nonce 已使用: {approval_nonce}")
        changed = conn.execute(
            "UPDATE trading_confirmation SET status='REJECTED', approved_by=?, "
            "approval_channel=?, approval_nonce=? WHERE confirmation_id=? AND status='PENDING'",
            (rejected_by, approval_channel, approval_nonce, confirmation_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("Confirmation 并发拒绝失败")
        audit(conn, "CONFIRMATION_REJECTED", entity_type="confirmation",
              entity_id=confirmation_id,
              payload={"approved_by": rejected_by, "channel": approval_channel,
                       "nonce": approval_nonce, "reason": reason}, commit=False)
    result = get_confirmation(conn, confirmation_id)
    assert result is not None
    return result


def approved_confirmation(conn: sqlite3.Connection,
                          confirmation_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM trading_confirmation WHERE confirmation_id=? AND status='APPROVED'",
        (confirmation_id,),
    ).fetchone()


def mark_plan_consumed(conn: sqlite3.Connection, confirmation_id: str,
                       plan_id: str) -> None:
    changed = conn.execute(
        "UPDATE trading_confirmation SET status='CONSUMED' "
        "WHERE confirmation_id=? AND status='APPROVED'", (confirmation_id,)
    ).rowcount
    if changed != 1:
        raise RuntimeError("Confirmation 未能原子消费")
    conn.execute(
        "UPDATE trading_execution_plan SET status='CONSUMED' WHERE plan_id=?", (plan_id,)
    )


def set_confirmation_status(conn: sqlite3.Connection, confirmation_id: str, status: str) -> None:
    if status not in CONFIRMATION_STATUSES:
        raise ValueError(f"非法 confirmation status: {status}")
    conn.execute("UPDATE trading_confirmation SET status = ? WHERE confirmation_id = ?",
                 (status, confirmation_id))
    conn.commit()


def insert_intent(conn: sqlite3.Connection, client_request_id: str, plan_id: str, plan_order_id: str,
                  symbol: str, side: str, quantity: float, order_type: str = "MARKET",
                  reference_price: Optional[float] = None, max_slippage_bps: Optional[float] = None,
                  status: str = "PENDING", strategy_version_id: Optional[int] = None,
                  confirmation_id: Optional[str] = None) -> None:
    """创建 OrderIntent（幂等身份 D-9）。

    用普通 INSERT（非 REPLACE）：client_request_id / (plan_id, plan_order_id) 的 UNIQUE
    约束冲突必须抛 IntegrityError——OrderManager 靠它在事务内实现"多订单全成或全败"原子性。
    幂等由 OrderManager 的"已有 intents 直接返回"检查保证，不在插入层静默吞冲突。

    注意：本函数**不 commit**——事务边界由业务层控制（OrderManager 的 BEGIN IMMEDIATE
    ... COMMIT/ROLLBACK）。db 层函数自动 commit 会破坏外层事务的原子性（见 test_5）。
    """
    if side not in ("BUY", "SELL"):
        raise ValueError(f"非法 side: {side}")
    conn.execute(
        "INSERT INTO trading_order_intent "
        "(client_request_id, plan_id, plan_order_id, symbol, side, quantity, order_type, "
        " reference_price, max_slippage_bps, strategy_version_id, confirmation_id, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (client_request_id, plan_id, plan_order_id, symbol, side, quantity, order_type,
         reference_price, max_slippage_bps, strategy_version_id, confirmation_id, status, _now()),
    )


def list_intents(conn: sqlite3.Connection, plan_id: Optional[str] = None) -> List[sqlite3.Row]:
    if plan_id:
        return conn.execute("SELECT * FROM trading_order_intent WHERE plan_id = ? ORDER BY plan_order_id",
                            (plan_id,)).fetchall()
    return conn.execute("SELECT * FROM trading_order_intent ORDER BY intent_id").fetchall()


def get_intent_by_request_id(conn: sqlite3.Connection, client_request_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM trading_order_intent WHERE client_request_id = ?",
                        (client_request_id,)).fetchone()


def get_intent_by_broker_order_id(conn: sqlite3.Connection,
                                  broker_order_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM trading_order_intent WHERE broker_order_id = ?",
        (broker_order_id,),
    ).fetchone()


def set_intent_status(conn: sqlite3.Connection, intent_id: int, status: str,
                      broker_order_id: Optional[str] = None,
                      commit: bool = True) -> None:
    if status not in INTENT_STATUSES:
        raise ValueError(f"非法 intent status: {status}")
    if broker_order_id:
        conn.execute("UPDATE trading_order_intent SET status = ?, broker_order_id = ? WHERE intent_id = ?",
                     (status, broker_order_id, intent_id))
    else:
        conn.execute("UPDATE trading_order_intent SET status = ? WHERE intent_id = ?", (status, intent_id))
    if commit:
        conn.commit()


def get_intent(conn: sqlite3.Connection, intent_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM trading_order_intent WHERE intent_id = ?", (intent_id,)).fetchone()


def upsert_broker_order(conn: sqlite3.Connection, intent_id: int,
                        broker_order_id: str, raw: Dict,
                        commit: bool = True) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO trading_broker_order "
        "(broker_order_id, intent_id, raw_json, updated_at) VALUES (?, ?, ?, ?)",
        (broker_order_id, intent_id,
         json.dumps(raw, ensure_ascii=False, sort_keys=True)[:4000], _now()),
    )
    if commit:
        conn.commit()


def insert_fill(conn: sqlite3.Connection, intent_id: int,
                broker_order_id: str, symbol: str, side: str,
                quantity: float, price: float, filled_at: Optional[str] = None,
                commit: bool = True) -> int:
    conn.execute(
        "INSERT INTO trading_fill "
        "(broker_order_id, intent_id, symbol, side, quantity, price, filled_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (broker_order_id, intent_id, symbol, side, quantity, price, filled_at or _now()),
    )
    fill_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    if commit:
        conn.commit()
    return int(fill_id)


def filled_quantity(conn: sqlite3.Connection, intent_id: int) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) AS quantity "
        "FROM trading_fill WHERE intent_id=?", (intent_id,)
    ).fetchone()
    return float(row["quantity"] if row else 0.0)


# ────────────────────────────────────────────────────────────────
# v4.0 MarketState（D-8 quote 新鲜度，P3）
# ────────────────────────────────────────────────────────────────

def upsert_market_state(conn: sqlite3.Connection, symbol: str, quote_at: str, price: float,
                        max_age_seconds: int = 300) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO trading_market_state (symbol, quote_at, price, max_age_seconds, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (symbol, quote_at, price, max_age_seconds, _now()),
    )
    conn.commit()


def get_market_state(conn: sqlite3.Connection, symbol: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM trading_market_state WHERE symbol = ?", (symbol,)).fetchone()


# ────────────────────────────────────────────────────────────────
# v4.0 DataHub 增量缓存辅助
# ────────────────────────────────────────────────────────────────

def get_last_bar(conn: sqlite3.Connection, symbol: str) -> Optional[sqlite3.Row]:
    """最后一根日线（增量缓存断点：从此 ts 之后拉新数据）。"""
    return conn.execute("SELECT ts, close FROM bars WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
                        (symbol,)).fetchone()


if __name__ == "__main__":
    # 冒烟测试
    conn = get_conn(":memory:")
    upsert_bars(conn, "TEST.US", [
        {"ts": "2024-01-02", "open": 100, "high": 105, "low": 99, "close": 104, "volume": 1_000_000},
        {"ts": "2024-01-03", "open": 104, "high": 106, "low": 101, "close": 102, "volume": 900_000},
    ], source="test")
    bars = get_bars(conn, "TEST.US")
    assert len(bars) == 2, f"bars 数量错误: {len(bars)}"
    set_lifecycle(conn, "TEST.US", "candidate")
    set_lifecycle(conn, "TEST.US", "degraded", evidence_hash="h1")
    set_lifecycle(conn, "TEST.US", "degraded", evidence_hash="h1")  # 同 hash 不累计
    assert get_lifecycle(conn, "TEST.US")["fail_count"] == 1
    set_lifecycle(conn, "TEST.US", "removed", evidence_hash="h2")  # 新证据 → +1
    assert get_lifecycle(conn, "TEST.US")["fail_count"] == 2
    print("db.py 冒烟测试通过 ✅")
