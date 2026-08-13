#!/usr/bin/env python3
"""
P3 Execution Safety — 13 条行为验收（架构 v4.0 硬门槛）
======================================================
验收全过之前系统必须保持 dry-run only。

验收清单（docs/architecture.md 执行安全模型）：
1.  无有效 Confirmation 无法提交任何订单
2.  Confirmation 与 plan_hash 强绑定（任何字段变化 → 失效；execution_mode 变化 → 失效）
3.  Confirmation 一次性原子消费（同一 DB 事务）
4.  重复执行同一 Plan 不重复创建任何 OrderIntent；每个 plan_order_id 最多对应一个 LIVE OrderIntent
5.  多订单 Plan 原子性：所有 OrderIntent 创建 + Confirmation 消费全成或全败
6.  提交中程序崩溃可恢复（不重复下单、不丢单）
7.  AccountState 非 SYNCED 时拒绝
8.  quote stale 时拒绝
9.  slippage 超限时拒绝
10. plan expired 时拒绝
11. 订单状态 UNKNOWN 时禁止新订单
12. broker/local 不一致时 fail closed
13. partial fill 能正确恢复状态（submitted/rejected/filled/cancelled/changed）
"""

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from shared import db as dbm
from shared.account import AccountState
from execution.models import (
    APPROVAL_PROOF_CHANNEL, ExecutionPlan, PlanOrder, Confirmation, MarketState,
    PreTradeRiskResult, now_utc,
)
from execution.order_manager import ConfirmationService, ApprovalAdapter, OrderManager
from execution.pretrade_risk import evaluate as pretrade_evaluate
from execution.broker import BrokerEventHandler, Reconciliation, normalize_broker_status


# ────────────────────────────────────────────────────────────────
# fixtures / helpers
# ────────────────────────────────────────────────────────────────

def future(days: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def past(days: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_plan(plan_id="p1", mode="DRY_RUN", orders=None, expires=future(1)):
    orders = orders or [PlanOrder("1", "NVDA.US", "BUY", 10, reference_price=223.96)]
    return ExecutionPlan(plan_id=plan_id, account_id="default", execution_mode=mode,
                         expires_at=expires, orders=orders)


def fresh_state(symbol="NVDA.US", price=224.0):
    return MarketState(symbol=symbol, quote_at=now_utc(), price=price, max_age_seconds=300)


@pytest.fixture
def conn():
    return dbm.get_conn(":memory:")


@pytest.fixture
def approved_plan(conn):
    """标准通过链：plan → PENDING → APPROVED。返回 (plan, confirmation)。"""
    plan = make_plan()
    svc = ConfirmationService(conn)
    adapter = ApprovalAdapter(conn, channel="cli")
    cfm = svc.create(plan)
    approved = adapter.approve(cfm.confirmation_id, approved_by="owner", nonce="n1")
    return plan, approved


def ok_account():
    return AccountState(account_id="default", sync_status="SYNCED", cash=100000.0, buying_power=50000.0)


def ok_risk(plan, cfm, account=None, states=None):
    return pretrade_evaluate(plan, cfm, account or ok_account(),
                             states or {o.symbol: fresh_state(o.symbol) for o in plan.orders})


# ────────────────────────────────────────────────────────────────
# 验收 1：无有效 Confirmation 无法提交任何订单
# ────────────────────────────────────────────────────────────────

def test_1_no_confirmation_no_order(conn):
    om = OrderManager(conn)
    plan = make_plan()
    # 无 confirmation
    with pytest.raises(AssertionError):
        om.consume(plan, Confirmation(confirmation_id="x", plan_id="p1",
                                      plan_hash=plan.plan_hash, status="PENDING"))
    assert dbm.list_intents(conn, "p1") == []
    # PENDING 未批准也不可消费
    svc = ConfirmationService(conn)
    cfm = svc.create(plan)
    with pytest.raises(AssertionError):
        om.consume(plan, cfm)
    assert dbm.list_intents(conn, "p1") == []


# ────────────────────────────────────────────────────────────────
# 验收 2：Confirmation 与 plan_hash 强绑定
# ────────────────────────────────────────────────────────────────

def test_2_plan_hash_binding(conn, approved_plan):
    plan, approved = approved_plan
    om = OrderManager(conn)
    # 任何字段变化 → plan_hash 变 → 原 confirmation 失效
    changed_orders = [PlanOrder("1", "NVDA.US", "BUY", 999, reference_price=223.96)]  # 数量变了
    changed_plan = make_plan(plan_id="p_changed", orders=changed_orders)
    assert changed_plan.plan_hash != approved.plan_hash
    # 用原 confirmation（hash 绑旧 plan）消费新 plan → 拒绝
    tampered = Confirmation(confirmation_id="cfm_t", plan_id=changed_plan.plan_id,
                            plan_hash=approved.plan_hash, status="APPROVED")
    with pytest.raises(RuntimeError):
        om.consume(changed_plan, tampered)
    assert dbm.list_intents(conn, "p_changed") == []

    # execution_mode 变化 → 失效（DRY_RUN → LIVE 必须新 plan + 新确认）
    live_plan = make_plan(plan_id="p_live", mode="LIVE")
    assert live_plan.plan_hash != approved.plan_hash
    # PreTradeRisk 层面：用旧 hash 的 confirmation → REJECT
    res = ok_risk(live_plan, Confirmation(confirmation_id="cfm_l", plan_id="p_live",
                                          plan_hash=approved.plan_hash, status="APPROVED"))
    assert res.decision == "REJECT"


def test_plan_orders_are_immutable_and_confirmation_cannot_cross_plans(conn):
    plan = make_plan(plan_id="p_bound")
    assert isinstance(plan.orders, tuple)
    with pytest.raises(AttributeError):
        plan.orders.append(PlanOrder("2", "KO.US", "BUY", 1))

    svc = ConfirmationService(conn)
    approved = ApprovalAdapter(conn).approve(
        svc.create(plan).confirmation_id, approved_by="owner", nonce="n_bound")
    other = make_plan(plan_id="p_other_bound")
    # 内容相同会得到相同 hash，但 plan_id 仍必须严格绑定。
    assert other.plan_hash == plan.plan_hash
    forged = Confirmation(
        confirmation_id=approved.confirmation_id,
        plan_id=other.plan_id,
        plan_hash=approved.plan_hash,
        status="APPROVED",
    )
    with pytest.raises(RuntimeError, match="plan_id"):
        OrderManager(conn).consume(other, forged)
    assert dbm.list_intents(conn, other.plan_id) == []


def test_persisted_plan_and_consumed_confirmation_cannot_be_overwritten(conn):
    plan = make_plan(plan_id="p_no_replace")
    svc = ConfirmationService(conn)
    pending = svc.create(plan, confirmation_id="cfm_no_replace")

    changed = make_plan(
        plan_id=plan.plan_id,
        orders=[PlanOrder("1", "NVDA.US", "BUY", 999, reference_price=223.96)],
    )
    with pytest.raises(ValueError, match="不可变内容"):
        dbm.insert_plan(conn, changed.plan_id, changed.account_id,
                        changed.execution_mode, changed.expires_at,
                        changed.plan_hash, [o.to_dict() for o in changed.orders])
    assert dbm.get_plan(conn, plan.plan_id)["plan_hash"] == plan.plan_hash

    approved = ApprovalAdapter(conn).approve(
        pending.confirmation_id, approved_by="owner", nonce="n_no_replace")
    OrderManager(conn).consume(plan, approved)
    with pytest.raises(ValueError, match="状态不可迁移"):
        dbm.insert_confirmation(
            conn, pending.confirmation_id, plan.plan_id, plan.plan_hash,
            expires_at=pending.expires_at, status="PENDING")
    assert dbm.get_confirmation(conn, pending.confirmation_id)["status"] == "CONSUMED"


# ────────────────────────────────────────────────────────────────
# 验收 3：Confirmation 一次性原子消费
# ────────────────────────────────────────────────────────────────

def test_3_confirmation_single_consume(conn, approved_plan):
    plan, approved = approved_plan
    om = OrderManager(conn)
    om.consume(plan, approved)
    row = dbm.get_confirmation(conn, approved.confirmation_id)
    assert row["status"] == "CONSUMED"
    assert len(dbm.list_intents(conn, plan.plan_id)) == 1
    # 再次消费同一 confirmation → 幂等返回已有，不报错不重复（同一 DB 事务内已锁定）
    again = om.consume(plan, approved)
    assert len(again) == 1
    assert len(dbm.list_intents(conn, plan.plan_id)) == 1


# ────────────────────────────────────────────────────────────────
# 验收 4：重复执行不重复创建；每 plan_order_id 最多一个 LIVE intent
# ────────────────────────────────────────────────────────────────

def test_4_idempotent_no_duplicate_intents(conn, approved_plan):
    plan, approved = approved_plan
    om = OrderManager(conn)
    for _ in range(3):  # 重复执行 3 次
        om.consume(plan, approved)
    intents = dbm.list_intents(conn, plan.plan_id)
    assert len(intents) == 1
    ids = [i["plan_order_id"] for i in intents]
    assert len(ids) == len(set(ids))  # plan_order_id 唯一


# ────────────────────────────────────────────────────────────────
# 验收 5：多订单 Plan 原子性（全成或全败）
# ────────────────────────────────────────────────────────────────

def test_5_multi_order_atomicity(conn):
    om = OrderManager(conn)
    plan = make_plan(plan_id="p_multi", orders=[
        PlanOrder("1", "NVDA.US", "BUY", 10, reference_price=223.96),
        PlanOrder("2", "KO.US", "BUY", 20, reference_price=86.83),
        PlanOrder("3", "SHV.US", "BUY", 30, reference_price=110.10),
    ])
    svc = ConfirmationService(conn)
    approved = svc.create(plan)
    approved = ApprovalAdapter(conn).approve(approved.confirmation_id, approved_by="owner", nonce="n_m")

    # 制造第 2 单插入冲突：预先占用 client_request_id="cr_p_multi_2"
    #（UNIQUE 约束冲突发生在插入时，而非幂等短路的"已有 intent"场景）
    dbm.insert_intent(conn, "cr_p_multi_2", "p_other", "x", "KO.US", "BUY", 20)
    with pytest.raises(Exception):
        om.consume(plan, approved)
    # 全败：事务回滚 → 无任何新 intent、confirmation 未消费
    assert dbm.list_intents(conn, "p_multi") == [], "原子性失败：部分 intent 已创建"
    assert dbm.get_confirmation(conn, approved.confirmation_id)["status"] == "APPROVED"


# ────────────────────────────────────────────────────────────────
# 验收 6：崩溃恢复（不重复下单、不丢单）
# ────────────────────────────────────────────────────────────────

def test_6_crash_recovery(conn, approved_plan):
    plan, approved = approved_plan
    om = OrderManager(conn)
    om.consume(plan, approved)  # 事务已提交后模拟崩溃
    recovered = om.recover(plan.plan_id)
    assert len(recovered) == 1
    # 再次 recover 幂等
    assert len(om.recover(plan.plan_id)) == 1
    # 不丢单：intent 可继续走 broker 状态机
    eh = BrokerEventHandler(conn)
    intent = dbm.list_intents(conn, plan.plan_id)[0]
    eh.handle({"type": "submitted", "intent_id": intent["intent_id"], "broker_order_id": "bo_r"})
    assert dbm.get_intent(conn, intent["intent_id"])["status"] == "SUBMITTED"


# ────────────────────────────────────────────────────────────────
# 验收 7：AccountState 非 SYNCED 拒绝
# ────────────────────────────────────────────────────────────────

def test_7_account_not_synced_rejected(conn, approved_plan):
    plan, approved = approved_plan
    states = {o.symbol: fresh_state(o.symbol) for o in plan.orders}
    for bad_status in ("STALE", "RECONCILING", "MISMATCH", "UNKNOWN"):
        acc = AccountState(account_id="default", sync_status=bad_status, buying_power=50000.0)
        res = pretrade_evaluate(plan, approved, acc, states)
        assert res.decision == "REJECT", f"sync_status={bad_status} 应 REJECT"
        assert any("SYNCED" in r for r in res.reasons)
    # SYNCED → PASS
    res = pretrade_evaluate(plan, approved, ok_account(), states)
    assert res.decision == "PASS"


def test_live_stale_account_closes_canary_before_submit(conn):
    plan = make_plan(plan_id="p_live_stale", mode="LIVE")
    svc = ConfirmationService(conn)
    cfm = svc.create(plan)
    approved = Confirmation(
        cfm.confirmation_id, plan.plan_id, plan.plan_hash, status="APPROVED",
        approval_channel=APPROVAL_PROOF_CHANNEL,
    )
    dbm.set_confirmation_status(conn, cfm.confirmation_id, "APPROVED")
    conn.execute(
        "UPDATE trading_confirmation SET approval_channel=? WHERE confirmation_id=?",
        (APPROVAL_PROOF_CHANNEL, cfm.confirmation_id),
    )
    conn.commit()
    dbm.mark_system_readiness(conn, "P0_A", "suite-hash")
    dbm.create_live_canary(conn, "default", ["NVDA.US"], "BUY", 1000, 1,
                           future(1), "canary_stale")
    class MockLiveBroker:
        enable_live = True
    with pytest.raises(RuntimeError, match="PreTradeRisk REJECT"):
        OrderManager(conn, broker=MockLiveBroker()).submit(
            plan, approved,
            market_states={o.symbol: fresh_state(o.symbol) for o in plan.orders},
            account_state=AccountState(account_id="default", sync_status="STALE",
                                       buying_power=50000.0),
        )
    row = conn.execute("SELECT * FROM live_canary").fetchone()
    assert row["status"] == "CLOSED"
    assert row["close_reason"] == "account_state_stale"


def test_live_unknown_existing_intent_closes_canary(conn):
    plan = make_plan(plan_id="p_live_unknown", mode="LIVE")
    dbm.insert_plan(conn, plan.plan_id, plan.account_id, plan.execution_mode,
                    plan.expires_at, plan.plan_hash,
                    [order.to_dict() for order in plan.orders])
    dbm.insert_intent(conn, "cr_unknown", plan.plan_id, "1", "NVDA.US", "BUY", 1,
                      status="UNKNOWN")
    dbm.mark_system_readiness(conn, "P0_A", "suite-hash")
    dbm.create_live_canary(conn, "default", ["NVDA.US"], "BUY", 1000, 1,
                           future(1), "canary_unknown")
    with pytest.raises(Exception, match="UNKNOWN"):
        OrderManager(conn).submit(
            plan, Confirmation("c", plan.plan_id, plan.plan_hash, status="APPROVED",
                               approval_channel="approval-proof"),
            market_states={o.symbol: fresh_state(o.symbol) for o in plan.orders},
            account_state=ok_account(),
        )
    row = conn.execute("SELECT * FROM live_canary").fetchone()
    assert row["status"] == "CLOSED"
    assert row["close_reason"] == "order_intent_unknown"


# ────────────────────────────────────────────────────────────────
# 验收 8：quote stale 拒绝
# ────────────────────────────────────────────────────────────────

def test_8_stale_quote_rejected(conn, approved_plan):
    plan, approved = approved_plan
    stale = MarketState(symbol="NVDA.US", quote_at=past(days=1), price=224.0, max_age_seconds=300)
    res = pretrade_evaluate(plan, approved, ok_account(), {"NVDA.US": stale})
    assert res.decision == "REJECT"
    assert any("quote" in r.lower() or "过期" in r for r in res.reasons)


# ────────────────────────────────────────────────────────────────
# 验收 9：slippage 超限拒绝
# ────────────────────────────────────────────────────────────────

def test_9_slippage_rejected(conn, approved_plan):
    plan, approved = approved_plan
    # 参考价 223.96，市场价 235 → 偏离 4700bps > 50bps
    far = fresh_state("NVDA.US", price=235.0)
    res = pretrade_evaluate(plan, approved, ok_account(), {"NVDA.US": far})
    assert res.decision == "REJECT"
    assert any("偏离" in r for r in res.reasons)
    # 市场价接近 → PASS
    near = fresh_state("NVDA.US", price=224.0)
    assert pretrade_evaluate(plan, approved, ok_account(), {"NVDA.US": near}).decision == "PASS"


# ────────────────────────────────────────────────────────────────
# 验收 10：plan expired 拒绝
# ────────────────────────────────────────────────────────────────

def test_10_expired_rejected(conn):
    om = OrderManager(conn)
    plan = make_plan(expires=past(1))
    svc = ConfirmationService(conn)
    cfm = svc.create(plan, expires_at=past(1))
    approved = ApprovalAdapter(conn).approve(cfm.confirmation_id, approved_by="owner", nonce="n_exp")
    # PreTradeRisk 拒绝
    res = ok_risk(plan, approved)
    assert res.decision == "REJECT"
    assert any("过期" in r for r in res.reasons)
    # OrderManager 也拒绝
    with pytest.raises(RuntimeError):
        om.consume(plan, approved)


# ────────────────────────────────────────────────────────────────
# 验收 11：订单状态 UNKNOWN 时禁止新订单
# ────────────────────────────────────────────────────────────────

def test_11_unknown_intent_blocks(conn, approved_plan):
    plan, approved = approved_plan
    res = pretrade_evaluate(plan, approved, ok_account(),
                            {o.symbol: fresh_state(o.symbol) for o in plan.orders},
                            unknown_intents=1)
    assert res.decision == "REJECT"
    assert any("UNKNOWN" in r for r in res.reasons)
    # 无 UNKNOWN → PASS
    assert ok_risk(plan, approved).decision == "PASS"


# ────────────────────────────────────────────────────────────────
# 验收 12：broker/local 不一致 fail closed
# ────────────────────────────────────────────────────────────────

def test_12_broker_local_mismatch_fail_closed(conn):
    # 本地 SUBMITTED 但 broker 查不到 → MISMATCH
    dbm.insert_intent(conn, "cr_mm_1", "p_mm", "1", "NVDA.US", "BUY", 10)
    intent = dbm.list_intents(conn, "p_mm")[0]
    BrokerEventHandler(conn).handle({"type": "submitted", "intent_id": intent["intent_id"],
                                     "broker_order_id": "bo_mm"})
    core_conn = dbm.get_core_conn(":memory:")
    dbm.upsert_account(core_conn, "default", "SYNCED")
    rec = Reconciliation(core_conn, conn, None)  # 无 broker → 查不到
    dbm.mark_system_readiness(conn, "P0_A", "suite-hash")
    dbm.create_live_canary(conn, "default", ["NVDA.US"], "BUY", 1000, 1,
                           future(1), "canary_mismatch")
    r = rec.reconcile_plan("p_mm")
    assert not r["ok"]
    assert dbm.get_account(core_conn, "default")["sync_status"] == "MISMATCH"
    canary = conn.execute("SELECT * FROM live_canary WHERE canary_id='canary_mismatch'").fetchone()
    assert canary["status"] == "CLOSED"
    assert canary["close_reason"] == "reconciliation_mismatch"
    # MISMATCH → 新计划 PreTradeRisk REJECT
    plan = make_plan(plan_id="p_after_mm")
    svc = ConfirmationService(conn)
    cfm = svc.create(plan)
    approved = ApprovalAdapter(conn).approve(cfm.confirmation_id, approved_by="owner", nonce="n_aft")
    res = pretrade_evaluate(plan, approved,
                            AccountState(account_id="default", sync_status="MISMATCH"),
                            {o.symbol: fresh_state(o.symbol) for o in plan.orders})
    assert res.decision == "REJECT"


def test_reconciliation_reads_sqlite_rows_and_accepts_matching_broker_state(conn):
    plan = make_plan(plan_id="p_rec")
    dbm.insert_plan(conn, plan.plan_id, plan.account_id, plan.execution_mode,
                    plan.expires_at, plan.plan_hash,
                    [order.to_dict() for order in plan.orders])
    dbm.insert_intent(conn, "cr_rec_1", plan.plan_id, "1", "NVDA.US", "BUY", 10)
    intent = dbm.list_intents(conn, plan.plan_id)[0]
    BrokerEventHandler(conn).handle({
        "type": "submitted", "intent_id": intent["intent_id"],
        "broker_order_id": "bo_rec",
    })

    class MatchingBroker:
        def order_state(self, broker_order_id):
            assert broker_order_id == "bo_rec"
            return {"status": "Submitted"}

    core_conn = dbm.get_core_conn(":memory:")
    result = Reconciliation(
        core_conn, conn, MatchingBroker()).reconcile_plan(plan.plan_id)
    assert result == {"ok": True, "mismatches": []}


@pytest.mark.parametrize("broker_status, expected", [
    ("NotReported", "SUBMITTED"),
    ("OrderStatus.WaitToNew", "SUBMITTED"),
    ("NewStatus", "SUBMITTED"),
    ("PendingReplaceStatus", "SUBMITTED"),
    ("PartialFilledStatus", "SUBMITTED"),
    ("WaitToCancel", "SUBMITTED"),
    ("FilledStatus", "FILLED"),
    ("CanceledStatus", "CANCELLED"),
    ("ExpiredStatus", "CANCELLED"),
    ("PartialWithdrawal", "CANCELLED"),
    ("RejectedStatus", "REJECTED"),
])
def test_longbridge_order_status_normalization(broker_status, expected):
    assert normalize_broker_status(broker_status) == expected


# ────────────────────────────────────────────────────────────────
# 验收 13：partial fill 正确恢复 + 事件处理全状态
# ────────────────────────────────────────────────────────────────

def test_13_partial_fill_and_all_events(conn):
    eh = BrokerEventHandler(conn)
    dbm.insert_intent(conn, "cr_pf_1", "p_pf", "1", "NVDA.US", "BUY", 10)
    iid = dbm.list_intents(conn, "p_pf")[0]["intent_id"]

    # submitted
    eh.handle({"type": "submitted", "intent_id": iid, "broker_order_id": "bo_pf"})
    assert dbm.get_intent(conn, iid)["status"] == "SUBMITTED"
    # partial fill 4/10 → 保持 SUBMITTED
    eh.handle({"type": "filled", "intent_id": iid, "broker_order_id": "bo_pf",
               "symbol": "NVDA.US", "side": "BUY", "quantity": 4, "price": 224.0})
    assert dbm.get_intent(conn, iid)["status"] == "SUBMITTED"
    assert eh.filled_quantity(iid) == 4
    # 补足 6/10 → FILLED
    eh.handle({"type": "filled", "intent_id": iid, "broker_order_id": "bo_pf",
               "symbol": "NVDA.US", "side": "BUY", "quantity": 6, "price": 224.5})
    assert dbm.get_intent(conn, iid)["status"] == "FILLED"
    assert eh.filled_quantity(iid) == 10

    # cancelled / changed
    dbm.insert_intent(conn, "cr_c_1", "p_c", "1", "KO.US", "BUY", 20)
    iid2 = dbm.list_intents(conn, "p_c")[0]["intent_id"]
    eh.handle({"type": "changed", "intent_id": iid2, "broker_order_id": "bo_c"})
    assert dbm.get_intent(conn, iid2)["status"] == "SUBMITTED"
    eh.handle({"type": "cancelled", "intent_id": iid2, "broker_order_id": "bo_c"})
    assert dbm.get_intent(conn, iid2)["status"] == "CANCELLED"

    # rejected
    dbm.insert_intent(conn, "cr_r_1", "p_r", "1", "SHV.US", "BUY", 30)
    iid3 = dbm.list_intents(conn, "p_r")[0]["intent_id"]
    eh.handle({"type": "rejected", "intent_id": iid3, "reason": "no_shares"})
    assert dbm.get_intent(conn, iid3)["status"] == "REJECTED"
