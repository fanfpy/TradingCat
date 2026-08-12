"""v5 Core/executiond 信任边界与 ApprovalProof 攻击测试。"""

import hashlib
import hmac
import time

import pytest

from execution.approval_wechat import HMACIdentityVerifier, IdentityProof
from execution.broker_live import LiveBroker
from execution.daemon import ExecutionDaemon
from execution.models import Confirmation, ExecutionPlan, PlanOrder, now_utc
from execution.order_manager import OrderManager
from execution.persistence import insert_plan
from execution.service import ExecutionService
from shared import db as dbm


SECRET = "v5-test-secret-at-least-thirty-two-characters"


def _plan(plan_id="p_boundary", mode="LIVE"):
    return ExecutionPlan(
        plan_id, "default", mode, "2099-12-31T23:59:59Z",
        (PlanOrder("1", "AAPL.US", "BUY", 1, reference_price=200.0),),
    )


def _persist_core(conn, plan):
    insert_plan(conn, plan.plan_id, plan.account_id, plan.execution_mode,
                plan.expires_at, plan.plan_hash,
                [order.to_dict() for order in plan.orders])


def _proof(verifier, cfm, nonce="proof-nonce", action="approve", timestamp=None):
    unsigned = IdentityProof("wechat-user", timestamp or int(time.time()), nonce, "")
    payload = verifier.payload(
        unsigned, action=action, confirmation_id=cfm.confirmation_id,
        plan_id=cfm.plan_id, plan_hash=cfm.plan_hash,
    )
    signature = hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return IdentityProof(
        unsigned.subject, unsigned.timestamp, nonce, signature,
        action=action, confirmation_id=cfm.confirmation_id,
        plan_id=cfm.plan_id, plan_hash=cfm.plan_hash,
    )


@pytest.fixture
def stores(tmp_path):
    core = dbm.get_core_conn(str(tmp_path / "core.db"))
    execution = dbm.get_execution_conn(str(tmp_path / "execution.db"))
    verifier = HMACIdentityVerifier(SECRET, {"wechat-user": "owner"})
    return core, execution, verifier


def test_core_forged_approved_confirmation_is_ignored(stores):
    core, execution, verifier = stores
    plan = _plan()
    _persist_core(core, plan)

    # 模拟 Agent/Core 已完全失陷：直接在 core.db 伪造 APPROVED。
    dbm.insert_confirmation(
        core, "cfm_attack", plan.plan_id, plan.plan_hash,
        expires_at="2099-12-31T23:59:59Z", approved_by="attacker",
        approval_channel="forged", approval_nonce="forged-nonce", status="APPROVED",
    )
    forged = Confirmation(**dict(dbm.get_confirmation(core, "cfm_attack")))

    service = ExecutionService(core, execution, identity_verifier=verifier)
    pending = service.request_confirmation(plan.plan_id, confirmation_id="cfm_real")
    assert pending.status == "PENDING"
    assert dbm.get_confirmation(execution, "cfm_attack") is None

    with pytest.raises((AssertionError, RuntimeError)):
        OrderManager(execution).consume(plan, forged)
    assert dbm.list_intents(execution) == []


def test_executiond_recomputes_hash_and_rejects_tampered_core_plan(stores):
    core, execution, verifier = stores
    plan = _plan()
    _persist_core(core, plan)
    core.execute(
        "UPDATE trading_execution_plan SET orders_json=? WHERE plan_id=?",
        ('[{"plan_order_id":"1","symbol":"AAPL.US","side":"BUY",'
         '"quantity":999,"order_type":"MARKET","reference_price":200.0,'
         '"reference_quote_at":null,"max_slippage_bps":50.0,'
         '"strategy_version_id":null}]', plan.plan_id),
    )
    core.commit()

    service = ExecutionService(core, execution, identity_verifier=verifier)
    with pytest.raises(RuntimeError, match="hash"):
        service.request_confirmation(plan.plan_id)
    assert dbm.get_plan(execution, plan.plan_id) is None


def test_only_valid_approval_proof_mints_execution_approval(stores):
    core, execution, verifier = stores
    plan = _plan()
    _persist_core(core, plan)
    service = ExecutionService(core, execution, identity_verifier=verifier)
    pending = service.request_confirmation(plan.plan_id)

    bad = IdentityProof("wechat-user", int(time.time()), "bad", "not-a-signature")
    with pytest.raises(ValueError):
        service.approve(pending.confirmation_id, bad)
    assert dbm.get_confirmation(execution, pending.confirmation_id)["status"] == "PENDING"

    approved = service.approve(pending.confirmation_id, _proof(verifier, pending))
    assert approved.status == "APPROVED"
    assert approved.approved_by == "owner"
    assert approved.approval_channel == "approval-proof"


def test_valid_execution_service_proof_can_consume_live_plan(stores):
    core, execution, verifier = stores
    plan = _plan("p_proof_consume")
    _persist_core(core, plan)
    service = ExecutionService(core, execution, identity_verifier=verifier)
    pending = service.request_confirmation(plan.plan_id)
    approved = service.approve(pending.confirmation_id, _proof(verifier, pending))

    created = OrderManager(execution).consume(plan, approved)

    assert len(created) == 1
    assert dbm.get_confirmation(execution, approved.confirmation_id)["status"] == "CONSUMED"
    assert dbm.list_intents(execution, plan.plan_id)[0]["status"] == "PENDING"


def test_approval_proof_nonce_replay_and_expiry_are_rejected(stores):
    core, execution, verifier = stores
    first = _plan("p_first")
    second = _plan("p_second")
    _persist_core(core, first)
    _persist_core(core, second)
    service = ExecutionService(core, execution, identity_verifier=verifier)
    cfm1 = service.request_confirmation(first.plan_id)
    cfm2 = service.request_confirmation(second.plan_id)
    service.approve(cfm1.confirmation_id, _proof(verifier, cfm1, nonce="same"))

    # 给第二张票据重新签名也不能复用已经消费过的 nonce。
    with pytest.raises(ValueError, match="nonce"):
        service.approve(cfm2.confirmation_id, _proof(verifier, cfm2, nonce="same"))
    expired = _proof(verifier, cfm2, nonce="expired", timestamp=int(time.time()) - 1000)
    with pytest.raises(ValueError, match="过期"):
        service.approve(cfm2.confirmation_id, expired)


def test_dry_run_confirmation_cannot_consume_live_plan(stores):
    core, execution, verifier = stores
    dry = _plan("p_dry", "DRY_RUN")
    live = _plan("p_live", "LIVE")
    _persist_core(core, dry)
    _persist_core(core, live)
    service = ExecutionService(core, execution, identity_verifier=verifier)
    dry_cfm = service.request_confirmation(dry.plan_id)
    dry_approved = service.approve(dry_cfm.confirmation_id, _proof(verifier, dry_cfm))
    service.read_and_snapshot_plan(live.plan_id)

    with pytest.raises(RuntimeError, match="plan_id"):
        OrderManager(execution).consume(live, dry_approved)
    assert dbm.list_intents(execution) == []


def test_live_rejects_same_physical_store(tmp_path):
    store = dbm.get_conn(str(tmp_path / "shared.db"))
    with pytest.raises(RuntimeError, match="物理隔离"):
        ExecutionService(store, store)


def _prepare_execute(stores, plan, *, proof_nonce="execute-proof"):
    core, execution, verifier = stores
    _persist_core(core, plan)
    service = ExecutionService(core, execution, identity_verifier=verifier)
    pending = service.request_confirmation(plan.plan_id)
    approved = service.approve(
        pending.confirmation_id,
        _proof(verifier, pending, nonce=proof_nonce),
    )
    dbm.upsert_account(
        core, plan.account_id, "SYNCED", cash=100_000, buying_power=100_000,
        nav=100_000,
    )
    dbm.upsert_market_state(core, "AAPL.US", now_utc(), 200.0,
                            max_age_seconds=10**9)
    return service, approved


def test_execute_accepts_identifiers_only_and_routes_paper_locally(stores):
    plan = _plan("p_execute_paper", "PAPER")
    service, approved = _prepare_execute(stores, plan)

    class StubService:
        def execute(self, **kwargs):
            raise AssertionError("execute with order overrides must be rejected first")

    daemon = object.__new__(ExecutionDaemon)
    daemon.service = StubService()
    with pytest.raises(ValueError, match="禁止订单字段覆盖"):
        daemon.dispatch({
            "operation": "execute", "plan_id": plan.plan_id,
            "confirmation_id": approved.confirmation_id, "quantity": 999,
        })

    result = service.execute(
        plan_id=plan.plan_id, confirmation_id=approved.confirmation_id,
    )
    assert result["status"] == "SUBMITTED"
    assert result["mode"] == "PAPER"
    assert dbm.list_intents(stores[1], plan.plan_id)[0]["broker_order_id"].startswith("paper_")


def test_execute_live_fails_closed_without_deployment_readiness(stores):
    plan = _plan("p_execute_live_gate", "LIVE")
    service, approved = _prepare_execute(stores, plan)
    broker = LiveBroker(stores[1], client=object(), enable_live=True,
                        kill_switch_engaged=False)
    service.broker = broker

    with pytest.raises(RuntimeError, match="P0_A readiness"):
        service.execute(
            plan_id=plan.plan_id, confirmation_id=approved.confirmation_id,
        )
    assert dbm.list_intents(stores[1], plan.plan_id) == []
    assert dbm.get_confirmation(stores[1], approved.confirmation_id)["status"] == "APPROVED"


def test_execute_returns_unknown_outcome_and_never_retries(stores):
    plan = _plan("p_execute_unknown", "LIVE")
    service, approved = _prepare_execute(stores, plan)
    dbm.mark_system_readiness(stores[1], "P0_A", "test-evidence")
    dbm.create_live_canary(
        stores[1], "default", ["AAPL.US"], "BUY", 1_000, 1,
        "2099-12-31T23:59:59Z", canary_id="execute-canary",
    )

    class FailingClient:
        calls = 0

        def order(self, **kwargs):
            self.calls += 1
            raise RuntimeError("network outcome unknown")

    client = FailingClient()
    service.broker = LiveBroker(
        stores[1], client=client, enable_live=True, kill_switch_engaged=False,
    )
    result = service.execute(
        plan_id=plan.plan_id, confirmation_id=approved.confirmation_id,
    )
    assert result["status"] == "UNKNOWN_OUTCOME"
    assert result["retry"] is False
    assert client.calls == 1
    assert dbm.list_intents(stores[1], plan.plan_id)[0]["status"] == "UNKNOWN"

    second = service.execute(
        plan_id=plan.plan_id, confirmation_id=approved.confirmation_id,
    )
    assert second["status"] == "UNKNOWN_OUTCOME"
    assert second["retry"] is False
    assert client.calls == 1


def test_daemon_forwards_request_claims_and_idempotency_key(stores):
    core, execution, verifier = stores
    plan = _plan("p_daemon_request", "LIVE")
    _persist_core(core, plan)
    service = ExecutionService(core, execution, identity_verifier=verifier)
    daemon = object.__new__(ExecutionDaemon)
    daemon.service = service

    first = daemon.dispatch({
        "operation": "request_confirmation", "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash, "idempotency_key": "request-1",
    })
    second = daemon.dispatch({
        "operation": "request_confirmation", "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash, "idempotency_key": "request-1",
    })
    assert first["confirmation_id"] == second["confirmation_id"]
    assert first["plan_hash"] == plan.plan_hash

    with pytest.raises(ValueError, match="plan_hash"):
        daemon.dispatch({
            "operation": "request_confirmation", "plan_id": plan.plan_id,
            "plan_hash": "tampered", "idempotency_key": "request-2",
        })


def test_daemon_approval_requires_and_binds_canonical_proof_claims(stores):
    core, execution, verifier = stores
    plan = _plan("p_daemon_approve", "LIVE")
    _persist_core(core, plan)
    service = ExecutionService(core, execution, identity_verifier=verifier)
    daemon = object.__new__(ExecutionDaemon)
    daemon.service = service
    pending = daemon.dispatch({
        "operation": "request_confirmation", "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash, "idempotency_key": "approve-1",
    })
    cfm = Confirmation(**pending)
    proof = _proof(verifier, cfm)
    raw = {
        "subject": proof.subject, "timestamp": proof.timestamp,
        "nonce": proof.nonce, "signature": proof.signature,
        "action": proof.action, "confirmation_id": proof.confirmation_id,
        "plan_id": proof.plan_id, "plan_hash": proof.plan_hash,
    }

    missing = dict(raw)
    del missing["plan_hash"]
    with pytest.raises(ValueError, match="canonical claims"):
        daemon.dispatch({
            "operation": "approve", "confirmation_id": cfm.confirmation_id,
            "approval_proof": missing,
        })
    assert dbm.get_confirmation(execution, cfm.confirmation_id)["status"] == "PENDING"

    tampered = dict(raw, plan_id="other-plan")
    with pytest.raises(ValueError, match="canonical claim|confirmation 不匹配|签名无效"):
        daemon.dispatch({
            "operation": "approve", "confirmation_id": cfm.confirmation_id,
            "approval_proof": tampered,
        })
    assert dbm.get_confirmation(execution, cfm.confirmation_id)["status"] == "PENDING"

    approved = daemon.dispatch({
        "operation": "approve", "confirmation_id": cfm.confirmation_id,
        "approval_proof": raw,
    })
    assert approved["status"] == "APPROVED"
