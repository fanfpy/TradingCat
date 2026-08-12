"""Minimal P0 regressions for execution event and recovery safety."""

from datetime import datetime, timedelta

from execution.broker import BrokerEventHandler, Reconciliation
from execution.models import Confirmation, ExecutionPlan, PlanOrder
from execution.order_manager import ConfirmationService, OrderManager
from execution.state import set_intent_status
from shared import db as dbm


def _intent(conn, plan_id="p_p0", qty=10):
    dbm.insert_intent(conn, f"cr_{plan_id}_1", plan_id, "1",
                      "AAPL.US", "BUY", qty)
    conn.commit()
    return dbm.list_intents(conn, plan_id)[0]


def test_fill_event_is_idempotent_and_capped():
    conn = dbm.get_execution_conn(":memory:")
    intent = _intent(conn)
    handler = BrokerEventHandler(conn)
    event = {"type": "filled", "intent_id": intent["intent_id"],
             "broker_order_id": "bo-1", "event_id": "trade-1",
             "symbol": "AAPL.US", "side": "BUY", "quantity": 8,
             "price": 100.0}

    handler.handle(event)
    handler.handle(event)
    assert handler.filled_quantity(intent["intent_id"]) == 8

    handler.handle({**event, "event_id": "trade-2", "quantity": 8})
    assert handler.filled_quantity(intent["intent_id"]) == 10
    assert dbm.get_intent(conn, intent["intent_id"])["status"] == "FILLED"


def test_terminal_intent_cannot_regress():
    conn = dbm.get_execution_conn(":memory:")
    intent = _intent(conn)
    set_intent_status(conn, intent["intent_id"], "FILLED")
    assert not set_intent_status(conn, intent["intent_id"], "SUBMITTED")
    assert dbm.get_intent(conn, intent["intent_id"])["status"] == "FILLED"


def test_late_fill_cannot_mutate_rejected_intent():
    conn = dbm.get_execution_conn(":memory:")
    intent = _intent(conn, plan_id="p_late")
    handler = BrokerEventHandler(conn)
    set_intent_status(conn, intent["intent_id"], "REJECTED")
    handler.handle({"type": "filled", "intent_id": intent["intent_id"],
                    "broker_order_id": "bo-late", "event_id": "late-1",
                    "symbol": "AAPL.US", "side": "BUY", "quantity": 10,
                    "price": 100.0})
    assert handler.filled_quantity(intent["intent_id"]) == 0
    assert dbm.get_intent(conn, intent["intent_id"])["status"] == "REJECTED"


def test_submitting_recovery_marks_unknown_without_resubmit():
    conn = dbm.get_execution_conn(":memory:")
    plan = ExecutionPlan(
        "p_recover", "default", "PAPER", "2099-12-31T23:59:59Z",
        (PlanOrder("1", "AAPL.US", "BUY", 1, reference_price=100.0),),
    )
    ConfirmationService(conn).create(plan)
    # Consume with a normal approval, then emulate the crash window.
    confirmations = conn.execute("SELECT * FROM trading_confirmation").fetchall()
    approved = dbm.approve_confirmation(
        conn, confirmations[0]["confirmation_id"], "owner", "cli", "recover-nonce")
    created = OrderManager(conn).consume(plan, Confirmation(**dict(approved)))
    intent = dbm.list_intents(conn, plan.plan_id)[0]
    set_intent_status(conn, intent["intent_id"], "SUBMITTING")

    class Broker:
        def __init__(self):
            self.calls = 0
        def submit_order(self, **kwargs):
            self.calls += 1
            raise AssertionError("SUBMITTING 恢复不得重复提交")

    broker = Broker()
    recovered = OrderManager(conn, broker=broker).recover(plan.plan_id)
    assert recovered[0]["status"] == "UNKNOWN"
    assert broker.calls == 0


def test_confirmation_expiry_is_exact_boundary():
    boundary = "2026-01-01T00:00:00Z"
    confirmation = Confirmation("c", "p", "h", expires_at=boundary)
    assert confirmation.is_expired(boundary)
    before = (datetime.fromisoformat(boundary.replace("Z", "+00:00")) -
              timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert not confirmation.is_expired(before)


def test_unknown_broker_status_fails_closed():
    conn = dbm.get_execution_conn(":memory:")
    intent = _intent(conn)
    BrokerEventHandler(conn).handle({
        "type": "submitted", "intent_id": intent["intent_id"],
        "broker_order_id": "bo-unknown",
    })

    class Broker:
        def order_state(self, broker_order_id):
            return {"status": "BROKER_NOT_SURE"}

    result = Reconciliation(dbm.get_core_conn(":memory:"), conn, Broker()).reconcile_plan("p_p0")
    assert not result["ok"]
    assert dbm.get_intent(conn, intent["intent_id"])["status"] == "UNKNOWN"
