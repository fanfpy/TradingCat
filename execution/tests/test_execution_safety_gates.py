from datetime import datetime, timedelta, timezone

import pytest

from execution.broker_live import LiveBroker, LiveBrokerSafetyError
from execution.models import (
    APPROVAL_PROOF_CHANNEL, Confirmation, ExecutionPlan, MarketState,
    PlanOrder, now_utc,
)
from execution.order_manager import ApprovalAdapter, ConfirmationService, OrderManager
from execution.order_router import DryRunRouter, LongbridgeRouter
from execution.pretrade_risk import RiskLimits, evaluate
from shared import db as dbm
from shared.account import AccountState


def future(days=1):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def approved(conn, mode="PAPER", qty=2):
    plan = ExecutionPlan(
        plan_id=f"p_{mode.lower()}", account_id="default", execution_mode=mode,
        expires_at=future(),
        orders=[PlanOrder("1", "AAPL.US", "BUY", qty, reference_price=100.0)],
    )
    pending = ConfirmationService(conn).create(plan)
    if mode == "LIVE":
        row = dbm.approve_confirmation(
            conn, pending.confirmation_id, "owner", APPROVAL_PROOF_CHANNEL,
            f"nonce_{mode.lower()}", expected_plan_id=plan.plan_id,
            expected_plan_hash=plan.plan_hash)
        return plan, Confirmation(**dict(row))
    return plan, ApprovalAdapter(conn).approve(
        pending.confirmation_id, approved_by="owner", nonce=f"nonce_{mode.lower()}"),


def risk_inputs(plan, **account_kwargs):
    account = AccountState(
        account_id="default", sync_status="SYNCED", cash=100_000.0,
        buying_power=100_000.0, nav=100_000.0,
        **account_kwargs,
    )
    states = {o.symbol: MarketState(o.symbol, now_utc(), o.reference_price or 100.0)
              for o in plan.orders}
    return account, states


def test_paper_is_local_and_idempotent(conn=None):
    conn = dbm.get_conn(":memory:")
    plan, confirmation = approved(conn, "PAPER")
    account, states = risk_inputs(plan)
    created = OrderManager(conn).submit(plan, confirmation,
                                        account_state=account, market_states=states)
    assert dbm.list_intents(conn, plan.plan_id)[0]["status"] == "SUBMITTED"
    assert dbm.list_intents(conn, plan.plan_id)[0]["broker_order_id"].startswith("paper_")
    assert any(row["event"] == "BROKER_PAPER_SUBMIT" for row in dbm.get_audit(conn))
    assert len(OrderManager(conn).submit(plan, confirmation,
                                         account_state=account, market_states=states)) == len(created)


def test_modes_cannot_fall_through_to_wrong_router():
    with pytest.raises(RuntimeError, match="只接受 DRY_RUN"):
        DryRunRouter().route({"client_request_id": "x"}, "PAPER")
    with pytest.raises(RuntimeError, match="PAPER"):
        LongbridgeRouter(enable_live=True).route({"client_request_id": "x"}, "PAPER")


def test_configured_limits_fail_closed_when_daily_loss_snapshot_missing(conn=None):
    conn = dbm.get_conn(":memory:")
    plan, confirmation = approved(conn, "PAPER", qty=2)
    account, states = risk_inputs(plan)
    result = evaluate(
        plan, confirmation, account, states,
        risk_limits=RiskLimits(max_order_notional_fraction=0.01,
                               max_daily_loss_fraction=0.02),
        daily_loss=None,
    )
    assert result.decision == "REJECT"
    assert any("日损失快照缺失" in reason for reason in result.reasons)


def test_position_and_portfolio_limits_are_hard_rejects(conn=None):
    conn = dbm.get_conn(":memory:")
    plan, confirmation = approved(conn, "PAPER", qty=20)
    account, states = risk_inputs(
        plan, positions=[{"symbol": "AAPL.US", "quantity": 90, "last_price": 100.0}])
    result = evaluate(
        plan, confirmation, account, states,
        risk_limits=RiskLimits(max_position_notional_fraction=0.10,
                               max_portfolio_notional_fraction=0.25,
                               max_daily_loss=1_000.0),
        daily_loss=0.0,
    )
    assert result.decision == "REJECT"
    assert any("单仓名义超限" in reason or "组合名义超限" in reason for reason in result.reasons)


def test_sell_without_position_is_rejected(conn=None):
    conn = dbm.get_conn(":memory:")
    plan = ExecutionPlan(
        plan_id="p_sell", account_id="default", execution_mode="PAPER",
        expires_at=future(),
        orders=[PlanOrder("1", "AAPL.US", "SELL", 1, reference_price=100.0)],
    )
    pending = ConfirmationService(conn).create(plan)
    confirmation = ApprovalAdapter(conn).approve(pending.confirmation_id, "owner", "nonce_sell")
    account, states = risk_inputs(plan)
    result = evaluate(plan, confirmation, account, states)
    assert result.decision == "REJECT"
    assert any("卖出仓位不足" in reason for reason in result.reasons)


def test_live_without_broker_does_not_consume_confirmation(conn=None):
    conn = dbm.get_conn(":memory:")
    plan, confirmation = approved(conn, "LIVE")
    account, states = risk_inputs(plan)
    with pytest.raises(RuntimeError, match="broker"):
        OrderManager(conn).submit(plan, confirmation,
                                  account_state=account, market_states=states)
    assert dbm.get_confirmation(conn, confirmation.confirmation_id)["status"] == "APPROVED"
    assert dbm.list_intents(conn, plan.plan_id) == []


def test_legacy_cli_approval_cannot_consume_or_submit_live(conn=None):
    conn = dbm.get_conn(":memory:")
    plan = ExecutionPlan(
        plan_id="p_cli_live", account_id="default", execution_mode="LIVE",
        expires_at=future(),
        orders=[PlanOrder("1", "AAPL.US", "BUY", 1, reference_price=100.0)],
    )
    pending = ConfirmationService(conn).create(plan)
    cli_approved = ApprovalAdapter(conn, channel="cli").approve(
        pending.confirmation_id, approved_by="owner", nonce="cli-live")
    with pytest.raises(RuntimeError, match="ApprovalProof"):
        OrderManager(conn).submit(plan, cli_approved)
    assert dbm.get_confirmation(conn, cli_approved.confirmation_id)["status"] == "APPROVED"
    assert dbm.list_intents(conn, plan.plan_id) == []


def test_legacy_cli_approval_cannot_reach_live_broker(conn=None):
    conn = dbm.get_conn(":memory:")
    plan = ExecutionPlan(
        plan_id="p_cli_broker", account_id="default", execution_mode="LIVE",
        expires_at=future(),
        orders=[PlanOrder("1", "AAPL.US", "BUY", 1, reference_price=100.0)],
    )
    pending = ConfirmationService(conn).create(plan)
    cli_approved = ApprovalAdapter(conn, channel="cli").approve(
        pending.confirmation_id, approved_by="owner", nonce="cli-broker")
    broker = LiveBroker(conn, client=_FakeClient(), enable_live=True,
                        kill_switch_engaged=False)
    with pytest.raises(LiveBrokerSafetyError, match="ApprovalProof"):
        broker.submit(
            {"client_request_id": "cr_p_cli_broker_1", "plan_order_id": "1",
             "symbol": "AAPL.US", "side": "BUY", "quantity": 1,
             "reference_price": 100.0},
            confirmation=cli_approved, plan=plan)


def test_legacy_adapter_cannot_mint_proof_channel(conn=None):
    conn = dbm.get_conn(":memory:")
    with pytest.raises(ValueError, match="ExecutionService"):
        ApprovalAdapter(conn, channel=APPROVAL_PROOF_CHANNEL)


class _FakeClient:
    def order(self, **kwargs):
        return {"success": True, "order_id": "fake-1"}


def test_live_kill_switch_blocks_before_client_call(conn=None):
    conn = dbm.get_conn(":memory:")
    plan, confirmation = approved(conn, "LIVE")
    broker = LiveBroker(conn, client=_FakeClient(), enable_live=True,
                        kill_switch_engaged=True)
    intent = {"client_request_id": "cr_p_live_1", "symbol": "AAPL.US",
              "side": "BUY", "quantity": 1, "reference_price": 100.0}
    with pytest.raises(LiveBrokerSafetyError, match="kill switch"):
        broker.submit(intent, confirmation=confirmation, plan=plan)


def test_expiry_boundary_is_fail_closed():
    plan = ExecutionPlan(
        plan_id="p_expiry_boundary", account_id="default", execution_mode="PAPER",
        expires_at="2026-01-01T00:00:00Z",
        orders=[PlanOrder("1", "AAPL.US", "BUY", 1, reference_price=100.0)],
    )
    assert plan.is_expired("2026-01-01T00:00:00Z")
