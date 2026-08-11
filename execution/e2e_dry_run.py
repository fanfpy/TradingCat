#!/usr/bin/env python3
"""
端到端 dry-run 演练 — 交易系统 v4.0（架构 D-3/D-8/D-9/D-12）
==========================================================
真实场景串起全链（默认 DRY_RUN，绝不触达券商）：

    Signal(NVDA 入场) → PositionSizer → ExecutionPlan → Confirmation(PENDING)
    → ApprovalAdapter(approve) → PreTradeRisk(PASS/REJECT) → OrderManager.consume
    → BrokerEventHandler(partial fill) → Reconciliation

用法：PYTHONPATH=. python3 execution/e2e_dry_run.py
退出码 0 = 演练成功。
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import db as dbm
from shared.account import AccountState
from execution.models import (
    ExecutionPlan, PlanOrder, Confirmation, MarketState, now_utc,
)
from execution.order_manager import ConfirmationService, ApprovalAdapter, OrderManager
from execution.pretrade_risk import evaluate as pretrade_evaluate
from execution.broker import BrokerEventHandler, Reconciliation


def main() -> int:
    core_conn = dbm.get_core_conn(":memory:")
    conn = dbm.get_execution_conn(":memory:")
    dbm.upsert_account(conn, "default", "SYNCED", cash=100_000.0, buying_power=80_000.0)
    account = AccountState(account_id="default", sync_status="SYNCED",
                           cash=100_000.0, buying_power=80_000.0)

    # 1. Signal → ExecutionPlan（NVDA 买入 10 股，8/7 收盘确认 223.96）
    plan = ExecutionPlan(
        plan_id="plan_nvda_001", account_id="default", execution_mode="DRY_RUN",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        orders=[PlanOrder("1", "NVDA.US", "BUY", 10,
                          reference_price=223.96, reference_quote_at=now_utc(),
                          max_slippage_bps=50)])
    print(f"[1] ExecutionPlan {plan.plan_id} mode={plan.execution_mode} hash={plan.plan_hash[:12]}…")

    # 2. ConfirmationService → PENDING
    svc = ConfirmationService(conn)
    cfm = svc.create(plan)
    print(f"[2] Confirmation {cfm.confirmation_id} status={cfm.status}")

    # 3. ApprovalAdapter（真实用户动作）→ APPROVED
    approved = ApprovalAdapter(conn, channel="cli").approve(
        cfm.confirmation_id, approved_by="owner", nonce="e2e_001")
    print(f"[3] APPROVED by={approved.approved_by} channel={approved.approval_channel}")

    # 4. PreTradeRisk：quote 新鲜 + 账户 SYNCED + 购买力足够 → PASS
    ms = MarketState(symbol="NVDA.US", quote_at=now_utc(), price=224.0, max_age_seconds=300)
    risk = pretrade_evaluate(plan, approved, account, {"NVDA.US": ms})
    assert risk.passed, f"PreTradeRisk 应 PASS: {risk.reasons}"
    print(f"[4] PreTradeRisk PASS")

    # 5. OrderManager 原子消费 → 1 个 OrderIntent + Confirmation CONSUMED
    om = OrderManager(conn)
    created = om.consume(plan, approved)
    assert len(created) == 1
    intent = dbm.list_intents(conn, plan.plan_id)[0]
    print(f"[5] OrderIntent created: {intent['client_request_id']} {intent['symbol']} "
          f"{intent['side']} {intent['quantity']} | confirmation={dbm.get_confirmation(conn, approved.confirmation_id)['status']}")

    # 6. BrokerEventHandler：submitted → partial fill 6/10 → 补足 FILLED
    eh = BrokerEventHandler(conn)
    eh.handle({"type": "submitted", "intent_id": intent["intent_id"], "broker_order_id": "bo_e2e"})
    eh.handle({"type": "filled", "intent_id": intent["intent_id"], "broker_order_id": "bo_e2e",
               "symbol": "NVDA.US", "side": "BUY", "quantity": 6, "price": 224.0})
    print(f"[6] partial fill: filled={eh.filled_quantity(intent['intent_id'])} "
          f"status={dbm.get_intent(conn, intent['intent_id'])['status']}")
    eh.handle({"type": "filled", "intent_id": intent["intent_id"], "broker_order_id": "bo_e2e",
               "symbol": "NVDA.US", "side": "BUY", "quantity": 4, "price": 224.2})
    print(f"    full fill: status={dbm.get_intent(conn, intent['intent_id'])['status']}")

    # 7. Reconciliation：终态 FILLED 无需对账 → ok
    rec = Reconciliation(core_conn, conn, None)
    r = rec.reconcile_plan(plan.plan_id)
    assert r["ok"], f"reconcile 应 ok: {r['mismatches']}"
    print(f"[7] Reconciliation ok")

    # 8. Audit trail 完整 lineage
    logs = dbm.get_audit(conn, entity_type="plan", entity_id=plan.plan_id)
    events = [l["event"] for l in logs]
    print(f"[8] Audit lineage: {events}")

    print("\n端到端 dry-run 演练通过（未触达券商，全程 DRY_RUN）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
