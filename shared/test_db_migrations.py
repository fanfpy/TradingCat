"""StateRepository schema migration regression tests."""

import sqlite3

from shared import db as dbm


def test_v4_database_is_upgraded_without_losing_rows(tmp_path):
    path = tmp_path / "legacy-v4.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        INSERT INTO schema_version VALUES (4, '2026-01-01T00:00:00Z');

        CREATE TABLE trading_account (
            account_id TEXT PRIMARY KEY,
            sync_status TEXT NOT NULL,
            cash REAL,
            buying_power REAL,
            updated_at TEXT NOT NULL,
            raw_json TEXT
        );
        INSERT INTO trading_account VALUES
            ('default', 'SYNCED', 1000, 900, '2026-01-01T00:00:00Z', '{}');

        CREATE TABLE strategy_version (
            version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            git_commit TEXT,
            code_hash TEXT,
            data_version TEXT,
            params_json TEXT,
            wf_report_json TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE trading_order_intent (
            intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_request_id TEXT NOT NULL UNIQUE,
            plan_id TEXT NOT NULL,
            plan_order_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity REAL NOT NULL,
            order_type TEXT NOT NULL,
            reference_price REAL,
            max_slippage_bps REAL,
            status TEXT NOT NULL,
            broker_order_id TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (plan_id, plan_order_id)
        );
        """
    )
    legacy.commit()
    legacy.close()

    conn = dbm.get_conn(str(path))
    try:
        account_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(trading_account)")
        }
        strategy_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(strategy_version)")
        }
        intent_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(trading_order_intent)")
        }
        assert "nav" in account_columns
        assert "oos_stats_json" in strategy_columns
        assert {"strategy_version_id", "confirmation_id"} <= intent_columns
        assert conn.execute(
            "SELECT cash FROM trading_account WHERE account_id='default'"
        ).fetchone()["cash"] == 1000
        assert conn.execute(
            "SELECT MAX(version) AS version FROM schema_version"
        ).fetchone()["version"] == dbm.SCHEMA_VERSION
    finally:
        conn.close()
