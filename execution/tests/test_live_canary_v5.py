from datetime import datetime, timedelta, timezone

import pytest

from shared import db as dbm


def _future():
    return (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_canary_cannot_open_before_p0a():
    conn = dbm.get_conn(":memory:")
    with pytest.raises(RuntimeError, match="P0-A"):
        dbm.create_live_canary(conn, "default", ["AAPL.US"], "BUY", 1000, 1, _future())


def test_canary_enforces_scope_budget_count_and_idempotency():
    conn = dbm.get_conn(":memory:")
    dbm.mark_system_readiness(conn, "P0_A", "suite-hash")
    row = dbm.create_live_canary(
        conn, "default", ["AAPL.US"], "BUY", 1000, 1, _future(), "canary_one")
    assert row["status"] == "ACTIVE"

    first = dict(account_id="default", plan_id="p1", client_request_id="cr1",
                 symbol="AAPL.US", side="BUY", quantity=2, reference_price=200)
    dbm.authorize_live_canary(conn, **first)
    dbm.authorize_live_canary(conn, **first)  # 同一 intent 重放不重复占额度
    current = conn.execute("SELECT * FROM live_canary WHERE canary_id='canary_one'").fetchone()
    assert current["used_orders"] == 1 and current["used_notional"] == 400

    with pytest.raises(RuntimeError, match="ACTIVE LIVE_CANARY"):
        dbm.authorize_live_canary(
            conn, account_id="default", plan_id="p2", client_request_id="cr2",
            symbol="MSFT.US", side="BUY", quantity=1, reference_price=100)
    with pytest.raises(RuntimeError, match="ACTIVE LIVE_CANARY"):
        dbm.authorize_live_canary(
            conn, account_id="default", plan_id="p3", client_request_id="cr3",
            symbol="AAPL.US", side="BUY", quantity=4, reference_price=200)


def test_unknown_closes_active_canary():
    conn = dbm.get_conn(":memory:")
    dbm.mark_system_readiness(conn, "P0_A", "suite-hash")
    dbm.create_live_canary(conn, "default", ["AAPL.US"], "BUY", 1000, 1, _future())
    assert dbm.close_live_canaries(conn, "order_unknown", "default") == 1
    row = conn.execute("SELECT * FROM live_canary").fetchone()
    assert row["status"] == "CLOSED" and row["close_reason"] == "order_unknown"
