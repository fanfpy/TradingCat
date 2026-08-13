import json
import hashlib
import hmac
import time

import pytest

from application import cli
from application.contracts import (
    ExecutiondClient, ExecutiondRPCError, TradingCatApplication,
)
from shared import db as dbm
from execution.approval_wechat import HMACIdentityVerifier
from execution.models import ExecutionPlan, PlanOrder


def test_unknown_operation_is_a_machine_readable_argument_error(capsys):
    assert cli.main(["not-a-real-operation"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["operation"] == "not-a-real-operation"
    assert result["error"]["code"] == "UNKNOWN_OPERATION"


def test_invalid_json_is_a_machine_readable_argument_error(capsys, monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", type("Input", (), {
        "read": lambda self: "{bad-json",
    })())
    assert cli.main(["status"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["operation"] == "Status"
    assert result["error"]["code"] == "ARGUMENT_ERROR"


def test_short_status_and_report_operations_use_the_same_envelope(monkeypatch):
    conn = dbm.get_conn(":memory:")
    monkeypatch.setattr(cli.dbm, "get_core_conn", lambda: conn)
    status = cli._invoke("status", {})
    report = cli._invoke("report", {})
    for result, operation in ((status, "Status"), (report, "Report")):
        assert result["schema_version"] == "tradingcat.v1"
        assert result["operation"] == operation
        assert result["ok"] is True
        assert set(result) == {
            "schema_version", "request_id", "operation", "ok", "data",
            "error", "warnings", "lineage",
        }
    assert conn.execute("SELECT count(*) FROM security_master").fetchone()[0] == 0


def test_propose_live_without_orders_is_not_approvable_or_persisted():
    app = TradingCatApplication(dbm.get_core_conn(":memory:"))
    result = app.propose_trade(100_000, mode="LIVE")
    assert result["ok"] is True
    assert result["data"]["status"] == "BLOCKED"
    assert result["data"]["error"]["code"] == "ACCOUNT_NOT_SYNCED"
    assert result["data"]["execution_plan"] is None
    assert result["data"]["requires_explicit_human_approval"] is False
    assert result["data"]["approval_status"] is None
    assert result["data"]["details"]["orders_count"] == 0
    assert app.core.execute(
        "SELECT count(*) FROM trading_execution_plan WHERE execution_mode='LIVE'"
    ).fetchone()[0] == 0


def test_propose_live_with_nonempty_orders_is_pending(monkeypatch):
    from execution.models import ExecutionPlan, PlanOrder
    from production.target_portfolio import TargetPortfolio
    from shared.account import AccountState

    core = dbm.get_core_conn(":memory:")
    app = TradingCatApplication(core)
    target = TargetPortfolio(
        intents=[], final_fracs={"AAPL.US": 0.01}, passed=True,
        details={"signal_count": 1, "research_statuses": {"AAPL.US": "verified"}},
    )
    plan = ExecutionPlan(
        "contract_live_nonempty", "default", "LIVE", "2099-12-31T23:59:59Z",
        (PlanOrder("1", "AAPL.US", "BUY", 1, reference_price=100.0),),
    )
    monkeypatch.setattr("production.decision.run_decision", lambda *args: target)
    monkeypatch.setattr(
        "production.decision.target_to_execution_plan",
        lambda *args, **kwargs: plan,
    )

    result = app.propose_trade(
        100_000, mode="LIVE", account_state=AccountState(sync_status="SYNCED"),
    )

    assert result["data"]["status"] == "PENDING_APPROVAL"
    assert result["data"]["execution_plan"]["orders"]
    assert result["data"]["requires_explicit_human_approval"] is True
    assert result["data"]["approval_status"] == "PENDING_APPROVAL"


def test_status_exposes_paper_default_and_live_pending_only():
    result = TradingCatApplication(dbm.get_core_conn(":memory:")).status()
    assert result["data"]["safety"] == {
        "default_mode": "PAPER", "paper_is_local": True,
        "live_enabled": False, "live_submission": "PENDING_APPROVAL_ONLY",
    }


def _live_plan(plan_id="cli_approve_plan", expires_at="2099-12-31T23:59:59Z"):
    return ExecutionPlan(
        plan_id, "default", "LIVE", expires_at,
        (PlanOrder("1", "AAPL.US", "BUY", 1, reference_price=100.0),),
    )


def _persist(conn, plan):
    dbm.insert_plan(conn, plan.plan_id, plan.account_id, plan.execution_mode,
                    plan.expires_at, plan.plan_hash,
                    [order.to_dict() for order in plan.orders])


def _canonical_proof(verifier, cfm, nonce="cli-nonce", timestamp=None):
    from execution.approval_wechat import IdentityProof
    unsigned = IdentityProof(
        "cli-owner", timestamp or int(time.time()), nonce, "",
        action="approve", confirmation_id=cfm.confirmation_id,
        plan_id=cfm.plan_id, plan_hash=cfm.plan_hash,
    )
    signature = hmac.new(
        verifier.secret,
        verifier.payload(unsigned, action="approve",
                         confirmation_id=cfm.confirmation_id,
                         plan_id=cfm.plan_id, plan_hash=cfm.plan_hash),
        hashlib.sha256,
    ).hexdigest()
    return {"subject": unsigned.subject, "action": "approve",
            "confirmation_id": cfm.confirmation_id, "plan_id": cfm.plan_id,
            "plan_hash": cfm.plan_hash, "nonce": nonce,
            "timestamp": unsigned.timestamp, "signature": signature}


def test_canonical_approve_mints_live_only_from_verified_proof(monkeypatch):
    core = dbm.get_core_conn(":memory:")
    execution = dbm.get_execution_conn(":memory:")
    secret = "c" * 32
    verifier = HMACIdentityVerifier(secret, {"cli-owner": "owner"})
    plan = _live_plan()
    _persist(core, plan)
    monkeypatch.setenv("TRADINGCAT_APPROVAL_IDENTITY_SECRET", secret)
    monkeypatch.setenv("TRADINGCAT_APPROVAL_OWNER_MAP", '{"cli-owner": "owner"}')
    from execution.service import ExecutionService
    pending = ExecutionService(core, execution, identity_verifier=verifier).request_confirmation(
        plan.plan_id, confirmation_id="cli_cfm")
    monkeypatch.setattr(cli.dbm, "get_core_conn", lambda: core)
    monkeypatch.setattr(cli.dbm, "get_execution_conn", lambda: execution)
    result = cli._invoke("approve", {
        "confirmation_id": pending.confirmation_id,
        "approval_proof": _canonical_proof(verifier, pending),
    })
    assert result["ok"] is True
    assert result["data"]["confirmation"]["status"] == "APPROVED"
    assert result["data"]["confirmation"]["approved_by"] == "owner"
    assert result["data"]["confirmation"]["approval_channel"] == "approval-proof"


def test_canonical_approve_rejects_string_approval_and_expired_confirmation(monkeypatch):
    core = dbm.get_core_conn(":memory:")
    execution = dbm.get_execution_conn(":memory:")
    # Keep the plan valid so this case isolates an expired confirmation.
    plan = _live_plan(expires_at="2099-12-31T23:59:59Z")
    _persist(core, plan)
    secret = "d" * 32
    verifier = HMACIdentityVerifier(secret, {"cli-owner": "owner"})
    monkeypatch.setenv("TRADINGCAT_APPROVAL_IDENTITY_SECRET", secret)
    monkeypatch.setenv("TRADINGCAT_APPROVAL_OWNER_MAP", '{"cli-owner": "owner"}')
    from execution.service import ExecutionService
    service = ExecutionService(core, execution, identity_verifier=verifier)
    pending = service.request_confirmation(plan.plan_id, confirmation_id="expired_cfm",
                                           expires_at="2000-01-01T00:00:00Z")
    monkeypatch.setattr(cli.dbm, "get_core_conn", lambda: core)
    monkeypatch.setattr(cli.dbm, "get_execution_conn", lambda: execution)
    with pytest.raises(cli.ContractArgumentError, match="不接受 approved_by"):
        cli._invoke("approve", {
            "confirmation_id": pending.confirmation_id,
            "approved_by": "owner",
        })
    from execution.service import ConfirmationExpiredError
    with pytest.raises(ConfirmationExpiredError):
        cli._invoke("approve", {
            "confirmation_id": pending.confirmation_id,
            "approval_proof": _canonical_proof(verifier, pending),
        })

    # A live plan expiry is reported before confirmation expiry.
    plan_only_expired = _live_plan(
        plan_id="plan_only_expired", expires_at="2000-01-01T00:00:00Z")
    _persist(core, plan_only_expired)
    pending_plan = service.request_confirmation(
        plan_only_expired.plan_id, confirmation_id="plan_expired_cfm",
        expires_at="2099-12-31T23:59:59Z")
    with pytest.raises(Exception) as exc_info:
        cli._invoke("approve", {
            "confirmation_id": pending_plan.confirmation_id,
            "approval_proof": _canonical_proof(verifier, pending_plan),
        })
    assert getattr(exc_info.value, "error_code", None) == "PLAN_EXPIRED"


class _FakeExecutiondSocket:
    def __init__(self, response):
        self.response = response
        self.sent = b""
        self.connected_to = None
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, path):
        self.connected_to = path

    def sendall(self, data):
        self.sent += data

    def recv(self, size):
        response, self.response = self.response, b""
        return response

    def close(self):
        self.closed = True


def test_execute_client_sends_identifiers_only_and_preserves_unknown_outcome():
    wire = json.dumps({"ok": True, "data": {
        "status": "UNKNOWN_OUTCOME", "plan_id": "plan-1",
        "confirmation_id": "confirmation-1", "retry": False,
        "message": "manual reconciliation required",
    }}).encode() + b"\n"
    fake_socket = _FakeExecutiondSocket(wire)
    client = ExecutiondClient("/tmp/executiond.sock", socket_factory=lambda *_: fake_socket)

    result = client.execute(plan_id="plan-1", confirmation_id="confirmation-1")

    assert result == {
        "status": "UNKNOWN_OUTCOME", "plan_id": "plan-1",
        "confirmation_id": "confirmation-1", "retry": False,
        "message": "manual reconciliation required",
    }
    assert json.loads(fake_socket.sent) == {
        "operation": "execute", "plan_id": "plan-1",
        "confirmation_id": "confirmation-1",
    }
    assert fake_socket.connected_to == "/tmp/executiond.sock"
    assert fake_socket.closed


def test_execute_client_returns_executiond_rejection_without_broker_fallback():
    fake_socket = _FakeExecutiondSocket(json.dumps({"ok": False, "error": {
        "type": "RuntimeError", "message": "confirmation 已消费，禁止自动重试",
    }}).encode() + b"\n")
    client = ExecutiondClient(socket_factory=lambda *_: fake_socket)

    with pytest.raises(ExecutiondRPCError, match="禁止自动重试"):
        client.execute(plan_id="plan-1", confirmation_id="confirmation-1")


def test_cli_execute_accepts_only_stable_identifier_input(monkeypatch):
    with pytest.raises(cli.ContractArgumentError, match="只允许"):
        cli._invoke("execute", {
            "plan_id": "plan-1", "confirmation_id": "confirmation-1",
            "quantity": 99,
        })

    class StubClient:
        def execute(self, *, plan_id, confirmation_id):
            assert (plan_id, confirmation_id) == ("plan-1", "confirmation-1")
            return {"status": "UNKNOWN_OUTCOME", "retry": False}

    monkeypatch.setattr("application.contracts.ExecutiondClient", lambda: StubClient())
    result = cli._invoke("execute", {
        "plan_id": "plan-1", "confirmation_id": "confirmation-1",
    })
    assert result["ok"] is True
    assert result["data"] == {"status": "UNKNOWN_OUTCOME", "retry": False}


def test_cli_execute_maps_executiond_unavailable_to_retryable_exit_one(monkeypatch, capsys):
    class OfflineClient:
        def execute(self, **kwargs):
            from application.contracts import ExecutiondUnavailableError
            raise ExecutiondUnavailableError("executiond offline")

    monkeypatch.setattr("application.contracts.ExecutiondClient", lambda: OfflineClient())
    monkeypatch.setattr(cli.sys, "stdin", type("Input", (), {
        "read": lambda self: '{"plan_id":"plan-1","confirmation_id":"confirmation-1"}',
    })())
    assert cli.main(["execute"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == {
        "code": "EXECUTIOND_UNAVAILABLE", "message": "executiond offline",
        "retryable": True,
    }
