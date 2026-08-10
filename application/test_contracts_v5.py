import json

from application.contracts import SCHEMA_VERSION, TradingCatApplication
from production.position import PositionIntent
from production.target_portfolio import TargetPortfolio
from shared import db as dbm


def test_chinese_security_resolution_and_follow_does_not_bind_strategy():
    conn = dbm.get_conn(":memory:")
    app = TradingCatApplication(conn)
    resolved = app.resolve_security("苹果")
    assert resolved["ok"] and resolved["data"]["symbol"] == "AAPL.US"
    followed = app.follow_security("Apple", reason="长期观察", channels=["wechat"])
    assert followed["ok"]
    assert followed["data"]["strategy_assignment"] is None
    assert followed["data"]["trade_eligible"] is False
    assert conn.execute("SELECT count(*) FROM strategy_assignment").fetchone()[0] == 0


def test_analyze_contract_marks_missing_fundamentals_without_fabrication():
    conn = dbm.get_conn(":memory:")
    result = TradingCatApplication(conn).analyze_security("苹果")
    assert result["schema_version"] == SCHEMA_VERSION and result["ok"]
    assert result["data"]["fundamental_factors"] is None
    assert any("PIT 基本面快照" in warning for warning in result["warnings"])
    assert result["data"]["trade_eligible"] is False


def test_two_agent_calls_produce_structurally_equivalent_response():
    conn = dbm.get_conn(":memory:")
    app = TradingCatApplication(conn)
    qwenpaw = app.analyze_security("AAPL")
    codex = app.analyze_security("苹果")
    for result in (qwenpaw, codex):
        result.pop("request_id")
    assert qwenpaw == codex


def test_request_approval_only_creates_pending_in_execution_store():
    core = dbm.get_conn(":memory:")
    execution = dbm.get_conn(":memory:")
    app = TradingCatApplication(core, execution)
    from execution.models import ExecutionPlan, PlanOrder
    plan = ExecutionPlan(
        "p_contract", "default", "DRY_RUN", "2099-12-31T23:59:59Z",
        (PlanOrder("1", "AAPL.US", "BUY", 1, reference_price=100),))
    dbm.insert_plan(core, plan.plan_id, plan.account_id, plan.execution_mode,
                    plan.expires_at, plan.plan_hash,
                    [order.to_dict() for order in plan.orders])
    result = app.request_approval(plan.plan_id)
    assert result["ok"] and result["data"]["approval_status"] == "PENDING"
    assert core.execute("SELECT count(*) FROM trading_confirmation").fetchone()[0] == 0
    assert execution.execute(
        "SELECT status FROM trading_confirmation").fetchone()[0] == "PENDING"


def test_investor_policy_version_is_frozen_into_plan_hash_lineage():
    conn = dbm.get_conn(":memory:")
    from production.decision import target_to_execution_plan
    v1 = dbm.get_active_investor_policy(conn, "default")
    intent = PositionIntent("AAPL.US", "BUY", 0.01, entry_price=100,
                            evidence={"strategy_version_id": 7})
    target = TargetPortfolio([intent], {"AAPL.US": 0.01}, True)
    first = target_to_execution_plan(conn, target, 100_000)
    v2 = dbm.save_investor_policy(conn, "default", {"max_single_position": 0.05})
    second = target_to_execution_plan(conn, target, 100_000)
    assert v1["policy_version_id"] != v2["policy_version_id"]
    assert first.orders[0].investor_policy_version_id == v1["policy_version_id"]
    assert second.orders[0].investor_policy_version_id == v2["policy_version_id"]
    assert first.plan_hash != second.plan_hash


def test_json_envelope_is_serializable():
    result = TradingCatApplication(dbm.get_conn(":memory:")).analyze_security("微软")
    assert json.loads(json.dumps(result, ensure_ascii=False))["operation"] == "AnalyzeSecurity"


def test_current_fundamentals_are_separate_from_historical_pit_factors():
    from shared.data_providers import (
        FundamentalProviderChain, FundamentalSnapshot, ProviderCapabilities,
    )

    class CurrentProvider:
        name = "current-test"

        def current_snapshot(self, symbol):
            return FundamentalSnapshot(
                symbol=symbol, source=self.name,
                observed_at="2026-08-10T00:00:00Z",
                values={"symbol": symbol, "current_roe": 0.25},
                capabilities=ProviderCapabilities(True, False, False, False),
            )

    conn = dbm.get_conn(":memory:")
    provider = FundamentalProviderChain([CurrentProvider()])
    result = TradingCatApplication(conn, fundamental_provider=provider).analyze_security("苹果")
    assert result["data"]["fundamental_factors"] is None
    current = result["data"]["current_fundamentals"]["snapshots"][0]
    assert current["values"]["current_roe"] == 0.25
    assert current["pit_safe"] is False
    assert dbm.fundamentals_as_of(conn, "AAPL.US", "2099-01-01") == []


def test_historical_as_of_never_calls_current_provider():
    class Explodes:
        def current(self, symbol):
            raise AssertionError("historical analysis must not fetch current data")

    result = TradingCatApplication(
        dbm.get_conn(":memory:"), fundamental_provider=Explodes()
    ).analyze_security("AAPL", as_of="2020-01-01")
    assert result["ok"]
    assert result["data"]["current_fundamentals"] is None
