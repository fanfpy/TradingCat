"""P3 运维入口：账户同步、风险检查、批量对账、版本查询与备份。"""

import argparse
from datetime import datetime, timedelta, timezone

import pytest

from execution.broker import BrokerEventHandler
from execution.models import ExecutionPlan, PlanOrder
from production.backup import run_daily
from production.operations import (
    check_current_portfolio,
    reconcile_runtime,
    sync_runtime_state,
)
from shared import db as dbm
from shared.account import AccountState
from tc import cmd_account, cmd_execution, cmd_strategy


class AccountBroker:
    def __init__(self, quantity=10):
        self.quantity = quantity

    def assets(self):
        return {"total_cash": 80_000, "max_finance_amount": 50_000,
                "net_assets": 100_000}

    def positions(self):
        return [{"symbol": "A.US", "quantity": self.quantity,
                 "cost_price": 90, "last_price": 100}]

    def orders(self):
        return []


def _position(conn):
    dbm.upsert_position(conn, "A.US", entry_price=90, entry_ts="2026-01-01",
                        quantity=10, stop_price=85, peak_high=105)
    dbm.upsert_bars(conn, "A.US", [{
        "ts": "2026-08-08", "open": 99, "high": 101, "low": 98,
        "close": 100, "volume": 1_000_000,
    }], "test")


def _submitted_plan(conn, plan_id="p_ops"):
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    plan = ExecutionPlan(plan_id, "default", "DRY_RUN", expires,
                         [PlanOrder("1", "A.US", "BUY", 10, reference_price=100)])
    dbm.insert_plan(conn, plan.plan_id, plan.account_id, plan.execution_mode,
                    plan.expires_at, plan.plan_hash,
                    [order.to_dict() for order in plan.orders])
    dbm.insert_intent(conn, f"cr_{plan_id}_1", plan_id, "1", "A.US", "BUY", 10)
    conn.commit()
    intent = dbm.list_intents(conn, plan_id)[0]
    BrokerEventHandler(conn).handle({
        "type": "submitted", "intent_id": intent["intent_id"],
        "broker_order_id": f"bo_{plan_id}",
    })
    return plan


def test_runtime_sync_passes_and_mismatch_fails_closed():
    conn = dbm.get_conn(":memory:")
    _position(conn)
    result = sync_runtime_state(conn, client=AccountBroker(quantity=10))
    assert result["ok"]
    assert result["account"]["sync_status"] == "SYNCED"
    assert result["account"]["nav"] == 100_000

    mismatch = sync_runtime_state(conn, client=AccountBroker(quantity=9))
    assert not mismatch["ok"]
    assert dbm.get_account(conn)["sync_status"] == "MISMATCH"


def test_position_query_failure_degrades_previously_synced_account():
    conn = dbm.get_conn(":memory:")
    dbm.upsert_account(conn, "default", "SYNCED", cash=80_000,
                       buying_power=50_000, nav=100_000)

    class FailingBroker:
        def positions(self):
            raise RuntimeError("position endpoint unavailable")

    args = argparse.Namespace(cmd="sync-positions", account_id="default")
    assert cmd_account(args, _conn=conn, _client=FailingBroker()) == 1
    assert dbm.get_account(conn)["sync_status"] == "STALE"


def test_current_risk_check_uses_real_snapshot_and_fails_on_stale_account():
    conn = dbm.get_conn(":memory:")
    _position(conn)
    dbm.upsert_account(conn, "default", "SYNCED", cash=80_000,
                       buying_power=50_000, nav=100_000)
    passed = check_current_portfolio(conn)
    assert passed["passed"]
    assert passed["positions_checked"] == 1

    stale = AccountState(account_id="default", sync_status="STALE",
                         cash=80_000, nav=100_000)
    rejected = check_current_portfolio(conn, account_state=stale)
    assert not rejected["passed"]
    assert rejected["failures"] == ["account_not_synced:STALE"]


def test_batch_reconciliation_restores_synced_or_sets_mismatch():
    conn = dbm.get_conn(":memory:")
    dbm.upsert_account(conn, "default", "SYNCED", cash=100_000,
                       buying_power=50_000, nav=100_000)
    plan = _submitted_plan(conn)

    class MatchingBroker:
        def order_state(self, broker_order_id):
            return {"status": "Submitted"}

    ok = reconcile_runtime(conn, MatchingBroker())
    assert ok["ok"] and ok["plans_checked"] == 1
    assert dbm.get_account(conn)["sync_status"] == "SYNCED"

    class MissingBroker:
        def order_state(self, broker_order_id):
            return None

    bad = reconcile_runtime(conn, MissingBroker(), plan_id=plan.plan_id)
    assert not bad["ok"]
    assert dbm.get_account(conn)["sync_status"] == "MISMATCH"
    assert dbm.list_intents(conn, plan.plan_id)[0]["status"] == "UNKNOWN"


def test_operational_cli_functions_are_machine_readable(capsys):
    conn = dbm.get_conn(":memory:")
    _position(conn)
    account_args = argparse.Namespace(cmd="sync", account_id="default")
    assert cmd_account(account_args, _conn=conn, _client=AccountBroker()) == 0
    assert '"ok": true' in capsys.readouterr().out

    dbm.save_strategy_version(conn, "A.US", "verified", oos_stats_json="{}")
    strategy_args = argparse.Namespace(cmd="list", symbol="A.US", limit=10)
    assert cmd_strategy(strategy_args, _conn=conn) == 0
    assert '"symbol": "A.US"' in capsys.readouterr().out

    _submitted_plan(conn, plan_id="p_cli_ops")
    execution_args = argparse.Namespace(cmd="reconcile", plan_id="p_cli_ops")

    class MatchingBroker:
        def order_state(self, broker_order_id):
            return {"status": "Submitted"}

    assert cmd_execution(execution_args, _conn=conn, _broker=MatchingBroker()) == 0
    assert '"ok": true' in capsys.readouterr().out


def test_daily_backup_accepts_explicit_source_database(tmp_path):
    source = tmp_path / "source.db"
    conn = dbm.get_conn(str(source))
    dbm.set_lifecycle(conn, "A.US", "candidate")
    conn.close()
    dest = tmp_path / "backup.db"

    result = run_daily(str(dest), db_path=str(source))
    assert result["path"] == str(dest)
    restored = dbm.get_conn(str(dest))
    try:
        assert dbm.get_lifecycle(restored, "A.US")["status"] == "candidate"
    finally:
        restored.close()


def test_backup_rejects_missing_source_and_same_file(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        dbm.backup(str(tmp_path / "out.db"), db_path=str(missing))

    source = tmp_path / "source.db"
    dbm.get_conn(str(source)).close()
    with pytest.raises(ValueError, match="不能与源数据库相同"):
        dbm.backup(str(source), db_path=str(source))
