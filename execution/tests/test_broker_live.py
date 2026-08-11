#!/usr/bin/env python3
"""
US-007 真实券商适配器 — mock 演练测试（execution/broker_live.py）
==================================================================
用 mock 券商（不触网）演练 submit/ack/partial fill/fill 全流程，验证
OrderManager → LiveBroker 衔接正确，以及安全边界：

1. DRY_RUN（默认）不触网：LiveBroker(enable_live=False) → mock 券商零调用
2. LIVE：OrderManager.submit → LiveBroker.submit_order(intent, confirmation, plan)
   → mock 券商真实调用 → ack(broker_order_id)
3. 安全边界：
   - enable_live=True 但无 confirmation/plan → LiveBrokerSafetyError（直通拒绝）
   - PENDING confirmation → 拒绝（必须 APPROVED）
   - APPROVED 但未消费（DB 行非 CONSUMED）→ 拒绝（绕过 OrderManager 链）
   - plan execution_mode 非 LIVE → 拒绝
4. 事件兼容接口：on_submitted / on_partial_fill / on_filled / on_rejected /
   on_cancelled / on_changed → 审计 + 委托 BrokerEventHandler 驱动状态机
   （partial fill 正确恢复：4/10 保持 SUBMITTED → 补足 10/10 FILLED）
5. 审计留痕：BROKER_DRY_RUN / BROKER_LIVE_SUBMIT / BROKER_EVENT
"""

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from shared import db as dbm
from shared.account import AccountState
from execution.models import (
    ExecutionPlan, PlanOrder, Confirmation, MarketState, now_utc,
)
from execution.order_manager import ConfirmationService, ApprovalAdapter, OrderManager
from execution.broker import BrokerEventHandler
from execution.broker_live import LiveBroker, BrokerAck, LiveBrokerSafetyError, LiveBrokerError


# ────────────────────────────────────────────────────────────────
# mock 券商（不触网）
# ────────────────────────────────────────────────────────────────

class FakeBrokerClient:
    """mock 券商：记录 order 调用，返回固定 order_id；不触网。"""

    def __init__(self):
        self.calls = []
        self._n = 0

    def order(self, side, symbol, qty, **kwargs):
        self._n += 1
        order_id = f"mock_{self._n}"
        self.calls.append({"side": side, "symbol": symbol, "qty": qty,
                           "order_id": order_id, **kwargs})
        return {"order_id": order_id, "symbol": symbol, "side": side,
                "quantity": qty, "order_type": kwargs.get("order_type", "LO"),
                "status": "Submitted", "success": True}

    def order_query(self, order_id):
        return {"order_id": order_id, "status": "Filled"}


# ────────────────────────────────────────────────────────────────
# fixtures / helpers
# ────────────────────────────────────────────────────────────────

def future(days: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_live_plan(plan_id="p_live", qty=10.0, symbol="NVDA.US"):
    return ExecutionPlan(
        plan_id=plan_id, account_id="default", execution_mode="LIVE",
        expires_at=future(1),
        orders=[PlanOrder("1", symbol, "BUY", qty, reference_price=223.96)])


def make_intent_dict(plan, client_request_id=None):
    return {"client_request_id": client_request_id or f"cr_{plan.plan_id}_1",
            "plan_id": plan.plan_id, "plan_order_id": "1",
            "symbol": plan.orders[0].symbol, "side": plan.orders[0].side,
            "quantity": plan.orders[0].quantity,
            "order_type": plan.orders[0].order_type,
            "reference_price": plan.orders[0].reference_price,
            "status": "PENDING"}


def risk_inputs(plan):
    account = AccountState(account_id="default", sync_status="SYNCED",
                           cash=100_000.0, buying_power=50_000.0)
    states = {o.symbol: MarketState(o.symbol, now_utc(), o.reference_price or 1.0)
              for o in plan.orders}
    return {"account_state": account, "market_states": states}


@pytest.fixture
def conn():
    c = dbm.get_conn(":memory:")
    dbm.mark_system_readiness(c, "P0_A", "pytest-p0-a")
    dbm.create_live_canary(
        c, "default", ["NVDA.US", "KO.US"], "BUY", 1_000_000, 100,
        future(10), canary_id="canary_pytest")
    return c


@pytest.fixture
def approved_plan(conn, plan_id="p_live"):
    """plan(LIVE) → PENDING → APPROVED（未消费）。"""
    plan = make_live_plan(plan_id=plan_id)
    svc = ConfirmationService(conn)
    cfm = svc.create(plan)
    approved = ApprovalAdapter(conn, channel="cli").approve(
        cfm.confirmation_id, approved_by="owner", nonce=f"n_{plan_id}")
    return plan, approved


# ────────────────────────────────────────────────────────────────
# 1. DRY_RUN 默认：不触网
# ────────────────────────────────────────────────────────────────

def test_dry_run_submit_no_network(conn, approved_plan):
    """默认 enable_live=False：mock 券商零调用，返回 DRY_RUN_SUBMITTED。"""
    plan, approved = approved_plan
    fake = FakeBrokerClient()
    broker = LiveBroker(conn, client=fake, enable_live=False)
    ack = broker.submit(make_intent_dict(plan), confirmation=approved, plan=plan)

    assert isinstance(ack, BrokerAck)
    assert ack.status == "DRY_RUN_SUBMITTED"
    assert ack.is_live is False
    assert ack.broker_order_id.startswith("dry_")
    assert fake.calls == [], "DRY_RUN 不应触达券商（mock 零调用）"
    # 审计留痕
    logs = [l for l in dbm.get_audit(conn, entity_type="intent")
            if l["event"] == "BROKER_DRY_RUN"]
    assert len(logs) == 1
    assert "dry_" in logs[0]["payload_json"]


def test_dry_run_constructor_default(conn, approved_plan):
    """未显式 enable_live → 构造后即 DRY_RUN，无需 client 也能 submit。"""
    plan, approved = approved_plan
    broker = LiveBroker(conn)  # 无 client、未 enable_live
    ack = broker.submit(make_intent_dict(plan), confirmation=approved, plan=plan)
    assert ack.status == "DRY_RUN_SUBMITTED"


def test_readonly_order_queries_do_not_enable_live_submission(conn, approved_plan):
    plan, approved = approved_plan
    fake = FakeBrokerClient()
    broker = LiveBroker(
        conn, client=fake, enable_live=False, enable_order_queries=True)

    assert broker.order_state("broker-1")["status"] == "Filled"
    assert broker.enable_live is False
    ack = broker.submit(make_intent_dict(plan), confirmation=approved, plan=plan)
    assert ack.status == "DRY_RUN_SUBMITTED"
    assert fake.calls == []


# ────────────────────────────────────────────────────────────────
# 2. LIVE：OrderManager → LiveBroker 全流程
# ────────────────────────────────────────────────────────────────

def test_live_submit_via_order_manager(conn, approved_plan):
    """OrderManager.submit(LIVE) → LiveBroker.submit_order → mock 券商调用 → ack。"""
    plan, approved = approved_plan
    fake = FakeBrokerClient()
    broker = LiveBroker(conn, client=fake, enable_live=True)
    om = OrderManager(conn, broker=broker)

    created = om.submit(plan, approved, **risk_inputs(plan))
    assert len(created) == 1

    # mock 券商收到一笔真实调用（侧/b方向/数量正确）
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["side"] == "buy"
    assert call["symbol"] == "NVDA.US"
    assert call["qty"] == 10
    assert call["order_type"] == "LO"
    # confirmation 已在链内消费（OrderManager.consume 原子消费）
    assert dbm.get_confirmation(conn, approved.confirmation_id)["status"] == "CONSUMED"
    # 审计留痕
    live_logs = [l for l in dbm.get_audit(conn, entity_type="intent")
                 if l["event"] == "BROKER_LIVE_SUBMIT"]
    assert len(live_logs) == 1
    assert "mock_1" in live_logs[0]["payload_json"]


def test_live_submit_ack_fields(conn, approved_plan):
    """LIVE ack 携带真实 broker_order_id + is_live=True。"""
    plan, approved = approved_plan
    OrderManager(conn).consume(plan, approved)  # 先走原子消费链
    fake = FakeBrokerClient()
    broker = LiveBroker(conn, client=fake, enable_live=True)
    ack = broker.submit(make_intent_dict(plan), confirmation=approved, plan=plan)
    assert ack.is_live is True
    assert ack.broker_order_id == "mock_1"
    assert ack.status == "SUBMITTED"
    assert ack.raw is not None and ack.raw.get("success") is True


def test_repeated_live_submit_never_calls_broker_twice(conn, approved_plan):
    plan, approved = approved_plan
    fake = FakeBrokerClient()
    om = OrderManager(conn, broker=LiveBroker(conn, client=fake, enable_live=True))

    first = om.submit(plan, approved, **risk_inputs(plan))
    second = om.submit(plan, approved, **risk_inputs(plan))

    assert len(first) == len(second) == 1
    assert len(fake.calls) == 1
    row = dbm.list_intents(conn, plan.plan_id)[0]
    assert row["status"] == "SUBMITTED"
    assert row["broker_order_id"] == "mock_1"


# ────────────────────────────────────────────────────────────────
# 3. 安全边界
# ────────────────────────────────────────────────────────────────

def test_live_without_confirmation_rejected(conn):
    """enable_live=True 但无 confirmation/plan → 拒绝，未触网。"""
    plan = make_live_plan()
    fake = FakeBrokerClient()
    broker = LiveBroker(conn, client=fake, enable_live=True)
    with pytest.raises(LiveBrokerSafetyError):
        broker.submit(make_intent_dict(plan))
    with pytest.raises(LiveBrokerSafetyError):
        broker.submit(make_intent_dict(plan), confirmation=None, plan=plan)
    assert fake.calls == [], "直通提交不应触达券商"


def test_live_with_pending_confirmation_rejected(conn):
    """PENDING（未批准）confirmation → 拒绝。"""
    plan = make_live_plan()
    svc = ConfirmationService(conn)
    pending = svc.create(plan)  # PENDING
    fake = FakeBrokerClient()
    broker = LiveBroker(conn, client=fake, enable_live=True)
    with pytest.raises(LiveBrokerSafetyError) as ei:
        broker.submit(make_intent_dict(plan), confirmation=pending, plan=plan)
    assert "APPROVED" in str(ei.value)
    assert fake.calls == []


def test_live_with_unconsumed_confirmation_rejected(conn, approved_plan):
    """APPROVED 但未消费（绕过 OrderManager.consume 直通）→ 拒绝。"""
    plan, approved = approved_plan  # APPROVED 但 DB 行仍 APPROVED（未 consume）
    fake = FakeBrokerClient()
    broker = LiveBroker(conn, client=fake, enable_live=True)
    with pytest.raises(LiveBrokerSafetyError) as ei:
        broker.submit(make_intent_dict(plan), confirmation=approved, plan=plan)
    assert "CONSUMED" in str(ei.value) and "直通" in str(ei.value)
    assert fake.calls == [], "直通提交不应触达券商"


def test_live_with_wrong_plan_hash_rejected(conn):
    """plan_hash 不匹配（计划被修改）→ 拒绝。"""
    plan = make_live_plan()
    svc = ConfirmationService(conn)
    cfm = svc.create(plan)
    approved = ApprovalAdapter(conn, channel="cli").approve(
        cfm.confirmation_id, approved_by="owner", nonce="n_hash")
    # 消费掉真实 plan 的 confirmation；伪造另一个 plan 用同一 confirmation 提交
    fake = FakeBrokerClient()
    broker = LiveBroker(conn, client=fake, enable_live=True)
    other_plan = make_live_plan(plan_id="p_other", qty=999.0)
    with pytest.raises(LiveBrokerSafetyError) as ei:
        broker.submit(make_intent_dict(other_plan), confirmation=approved, plan=other_plan)
    assert "plan_hash" in str(ei.value)
    assert fake.calls == []


def test_live_rejects_dry_run_plan(conn):
    """plan execution_mode != LIVE → 拒绝（LiveBroker 只接受 LIVE 计划）。"""
    plan = ExecutionPlan(
        plan_id="p_dry", account_id="default", execution_mode="DRY_RUN",
        expires_at=future(1),
        orders=[PlanOrder("1", "NVDA.US", "BUY", 10, reference_price=223.96)])
    svc = ConfirmationService(conn)
    cfm = svc.create(plan)
    approved = ApprovalAdapter(conn, channel="cli").approve(
        cfm.confirmation_id, approved_by="owner", nonce="n_dry")
    OrderManager(conn).consume(plan, approved)  # 消费链走完
    fake = FakeBrokerClient()
    broker = LiveBroker(conn, client=fake, enable_live=True)
    with pytest.raises(LiveBrokerSafetyError) as ei:
        broker.submit(make_intent_dict(plan), confirmation=approved, plan=plan)
    assert "execution_mode" in str(ei.value)
    assert fake.calls == []


# ────────────────────────────────────────────────────────────────
# 4. 事件兼容接口：submit/ack → partial fill → fill 全流程
# ────────────────────────────────────────────────────────────────

def _order_manager_live_broker(conn, fake, plan, approved):
    """构造经 OrderManager 消费的 LIVE broker（复用链）。返回 (broker, created, fake)。"""
    broker = LiveBroker(conn, client=fake, enable_live=True,
                        event_handler=BrokerEventHandler(conn))
    om = OrderManager(conn, broker=broker)
    created = om.submit(plan, approved, **risk_inputs(plan))
    return broker, created, fake


def test_full_flow_partial_fill_then_filled(conn, approved_plan):
    """全流程：submit ack → submitted → partial fill 4/10 → 补足 FILLED。"""
    plan, approved = approved_plan
    fake = FakeBrokerClient()
    broker, created, fake = _order_manager_live_broker(conn, fake, plan, approved)
    assert len(created) == 1
    assert len(fake.calls) == 1, "OrderManager.submit 应已触发一次券商提交"
    ack = BrokerAck(broker_order_id=fake.calls[0]["order_id"], status="SUBMITTED",
                    is_live=True)
    intent = dbm.list_intents(conn, plan.plan_id)[0]
    iid = intent["intent_id"]
    broker.on_submitted({"intent_id": iid, "broker_order_id": ack.broker_order_id})
    assert dbm.get_intent(conn, iid)["status"] == "SUBMITTED"
    assert dbm.get_intent(conn, iid)["broker_order_id"] == "mock_1"

    # partial fill 4/10 → 保持 SUBMITTED，已成交 4
    broker.on_partial_fill({"intent_id": iid, "broker_order_id": ack.broker_order_id,
                            "symbol": "NVDA.US", "side": "BUY",
                            "quantity": 4, "price": 224.0})
    assert dbm.get_intent(conn, iid)["status"] == "SUBMITTED"
    assert broker.event_handler.filled_quantity(iid) == 4

    # 补足 6/10 → FILLED，累计 10
    broker.on_filled({"intent_id": iid, "broker_order_id": ack.broker_order_id,
                      "symbol": "NVDA.US", "side": "BUY",
                      "quantity": 6, "price": 224.5})
    assert dbm.get_intent(conn, iid)["status"] == "FILLED"
    assert broker.event_handler.filled_quantity(iid) == 10

    # 事件全部落审计
    events = [l["payload_json"] for l in dbm.get_audit(conn, entity_type="intent")
              if l["event"] == "BROKER_EVENT"]
    assert len(events) == 3  # submitted + 2×filled(partial/full)
    assert all("BROKER_EVENT" not in e for e in events)  # payload 是事件内容


def test_polling_recovers_partial_and_final_fill_idempotently(conn, approved_plan):
    plan, approved = approved_plan
    fake = FakeBrokerClient()
    broker, _, _ = _order_manager_live_broker(conn, fake, plan, approved)
    row = dbm.list_intents(conn, plan.plan_id)[0]

    fake.order_query = lambda order_id: {
        "order_id": order_id, "status": "PartialFilled",
        "executed_quantity": 4, "executed_price": 224.0,
    }
    first = broker.poll_active_orders(plan.plan_id)
    assert first["updated"] == 1
    assert dbm.get_intent(conn, row["intent_id"])["status"] == "SUBMITTED"
    assert broker.event_handler.filled_quantity(row["intent_id"]) == 4

    fake.order_query = lambda order_id: {
        "order_id": order_id, "status": "Filled",
        "executed_quantity": 10, "executed_price": 224.5,
    }
    second = broker.poll_active_orders(plan.plan_id)
    assert second["updated"] == 1
    assert dbm.get_intent(conn, row["intent_id"])["status"] == "FILLED"
    assert broker.event_handler.filled_quantity(row["intent_id"]) == 10
    # 终态 intent 不再轮询，不会重复记 fill。
    third = broker.poll_active_orders(plan.plan_id)
    assert third["checked"] == 0
    assert broker.event_handler.filled_quantity(row["intent_id"]) == 10


def test_events_rejected_cancelled_changed(conn, approved_plan):
    """事件兼容：rejected / cancelled / changed 均落审计并驱动状态机。"""
    plan, approved = approved_plan
    fake = FakeBrokerClient()
    broker, _, fake = _order_manager_live_broker(conn, fake, plan, approved)
    iid = dbm.list_intents(conn, plan.plan_id)[0]["intent_id"]

    # changed → 保持 SUBMITTED
    broker.on_changed({"intent_id": iid, "broker_order_id": "mock_1",
                       "raw": {"qty": 8}})
    assert dbm.get_intent(conn, iid)["status"] == "SUBMITTED"
    # rejected → REJECTED
    broker.on_rejected({"intent_id": iid, "reason": "no_shares"})
    assert dbm.get_intent(conn, iid)["status"] == "REJECTED"

    # cancelled（新 intent）
    dbm.insert_intent(conn, "cr_c2", plan.plan_id, "2", "KO.US", "BUY", 20)
    iid2 = dbm.list_intents(conn, plan.plan_id)[1]["intent_id"]
    broker.on_cancelled({"intent_id": iid2, "broker_order_id": "mock_2"})
    assert dbm.get_intent(conn, iid2)["status"] == "CANCELLED"

    # 全部事件有审计
    events = [l["event"] for l in dbm.get_audit(conn, entity_type="intent")
              if l["event"] == "BROKER_EVENT"]
    assert len(events) >= 3


# ────────────────────────────────────────────────────────────────
# 5. 券商失败 → fail closed（不产生 ack）
# ────────────────────────────────────────────────────────────────

def test_live_submit_failure_fail_closed(conn, approved_plan):
    """mock 券商提交失败 → LiveBrokerError，不产生 ack。"""
    class FailingClient:
        def order(self, *args, **kwargs):
            raise RuntimeError("network down")

    plan, approved = approved_plan
    OrderManager(conn).consume(plan, approved)  # 先走原子消费链
    broker = LiveBroker(conn, client=FailingClient(), enable_live=True)
    with pytest.raises(LiveBrokerError) as ei:
        broker.submit(make_intent_dict(plan), confirmation=approved, plan=plan)
    assert "fail closed" in str(ei.value)


def test_order_manager_marks_uncertain_submit_unknown(conn, approved_plan):
    class FailingClient:
        def order(self, *args, **kwargs):
            raise RuntimeError("network down")

    plan, approved = approved_plan
    om = OrderManager(
        conn, broker=LiveBroker(conn, client=FailingClient(), enable_live=True))
    with pytest.raises(LiveBrokerError):
        om.submit(plan, approved, **risk_inputs(plan))

    row = dbm.list_intents(conn, plan.plan_id)[0]
    assert row["status"] == "UNKNOWN"
    # UNKNOWN 会让后续任何 LIVE 计划在 PreTradeRisk 处 fail closed。
    plan2 = make_live_plan(plan_id="p_after_unknown")
    svc = ConfirmationService(conn)
    approved2 = ApprovalAdapter(conn).approve(
        svc.create(plan2).confirmation_id, approved_by="owner", nonce="n_after_unknown")
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        om.submit(plan2, approved2, **risk_inputs(plan2))
