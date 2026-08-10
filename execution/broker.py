#!/usr/bin/env python3
"""
BrokerEventHandler + Reconciliation — 交易系统 v4.0（架构 P3）
============================================================
BrokerEventHandler：券商异步事件 → 本地 OrderIntent 状态机。

    事件类型：submitted / rejected / filled(含 partial) / cancelled / changed

状态机（每 intent）：
    PENDING → SUBMITTED → FILLED | REJECTED | CANCELLED
                     └→ PARTIAL(保留) → FILLED（partial fill 正确恢复）

- partial fill：fill 表逐笔记录已成交，quantity_filled 累计；未成交部分保持 SUBMITTED
- 订单状态 UNKNOWN 时禁止新订单（fail closed，D-9）

Reconciliation：broker/local 对账。
- 不一致（本地有 broker 无 / broker 有本地无 / 数量价格差异）→ fail closed：
  AccountState.sync_status = MISMATCH，且后续 PreTradeRisk 必然 REJECT
"""

from typing import Dict, List, Optional

from shared import db as dbm


def normalize_broker_status(status: object) -> str:
    """把长桥 SDK/HTTP/CLI 的订单状态归一到本地状态机。"""
    value = str(status or "UNKNOWN")
    if value.startswith("OrderStatus."):
        value = value.split(".", 1)[1]
    key = value.upper().replace("_", "")
    active = {
        "NOTREPORTED", "REPLACEDNOTREPORTED", "PROTECTEDNOTREPORTED",
        "VARIETIESNOTREPORTED", "WAITTONEW", "NEW", "NEWSTATUS",
        "WAITTOREPLACE", "PENDINGREPLACESTATUS", "REPLACEDSTATUS",
        "PARTIALFILLED", "PARTIALFILLEDSTATUS", "WAITTOCANCEL",
        "PENDINGCANCELSTATUS", "SUBMITTED",
    }
    if key in active:
        return "SUBMITTED"
    return {
        "FILLED": "FILLED", "FILLEDSTATUS": "FILLED",
        "CANCELED": "CANCELLED", "CANCELEDSTATUS": "CANCELLED",
        "CANCELLED": "CANCELLED", "EXPIRED": "CANCELLED",
        "EXPIREDSTATUS": "CANCELLED", "PARTIALWITHDRAWAL": "CANCELLED",
        "REJECTED": "REJECTED", "REJECTEDSTATUS": "REJECTED",
    }.get(key, key)


class BrokerEventHandler:
    def __init__(self, conn):
        self.conn = conn

    def handle(self, event: Dict) -> None:
        """处理一条券商事件。event: {type, broker_order_id, intent_id|client_request_id, ...}"""
        etype = event.get("type")
        handler = {
            "submitted": self._on_submitted,
            "rejected": self._on_rejected,
            "filled": self._on_filled,
            "cancelled": self._on_cancelled,
            "changed": self._on_changed,
        }.get(etype)
        if handler is None:
            raise ValueError(f"未知事件类型: {etype}")
        handler(event)

    # ── 状态转换 ────────────────────────────────────────────────

    def _on_submitted(self, e: Dict) -> None:
        intent_id = e["intent_id"]
        broker_order_id = e.get("broker_order_id")
        dbm.set_intent_status(self.conn, intent_id, "SUBMITTED", broker_order_id)
        self._upsert_broker_order(intent_id, broker_order_id, e)
        dbm.audit(self.conn, "BROKER_ORDER", entity_type="intent", entity_id=str(intent_id),
                  payload={"event": "submitted", "broker_order_id": broker_order_id})

    def _on_rejected(self, e: Dict) -> None:
        intent_id = e["intent_id"]
        dbm.set_intent_status(self.conn, intent_id, "REJECTED", e.get("broker_order_id"))
        self._upsert_broker_order(intent_id, e.get("broker_order_id"), e)
        dbm.audit(self.conn, "BROKER_ORDER", entity_type="intent", entity_id=str(intent_id),
                  payload={"event": "rejected", "reason": e.get("reason")})

    def _on_filled(self, e: Dict) -> None:
        """成交（含 partial）：记录 fill，累计已成交量。

        event: {type, intent_id, symbol, side, quantity(本次成交量), price, filled_at}
        """
        intent_id = e["intent_id"]
        with dbm.immediate_transaction(self.conn):
            if e.get("broker_order_id"):
                dbm.upsert_broker_order(
                    self.conn, intent_id, e["broker_order_id"], e, commit=False)
            dbm.insert_fill(
                self.conn, intent_id, e.get("broker_order_id") or f"fill_{intent_id}",
                e["symbol"], e["side"], e["quantity"], e["price"],
                e.get("filled_at"), commit=False)
            row = dbm.get_intent(self.conn, intent_id)
            total = row["quantity"]
            filled = self.filled_quantity(intent_id)
            status = "FILLED" if filled >= total - 1e-9 else "SUBMITTED"
            dbm.set_intent_status(
                self.conn, intent_id, status, e.get("broker_order_id"), commit=False)
            dbm.audit(self.conn, "FILL", entity_type="intent", entity_id=str(intent_id),
                      payload={"quantity": e["quantity"], "price": e["price"],
                               "filled_total": filled, "intent_status": status}, commit=False)

    def _on_cancelled(self, e: Dict) -> None:
        intent_id = e["intent_id"]
        dbm.set_intent_status(self.conn, intent_id, "CANCELLED", e.get("broker_order_id"))
        self._upsert_broker_order(intent_id, e.get("broker_order_id"), e)
        dbm.audit(self.conn, "BROKER_ORDER", entity_type="intent", entity_id=str(intent_id),
                  payload={"event": "cancelled"})

    def _on_changed(self, e: Dict) -> None:
        """券商回改（数量/价格）→ 更新 intent 参考信息 + 保持 SUBMITTED。"""
        intent_id = e["intent_id"]
        dbm.set_intent_status(self.conn, intent_id, "SUBMITTED", e.get("broker_order_id"))
        self._upsert_broker_order(intent_id, e.get("broker_order_id"), e)
        dbm.audit(self.conn, "BROKER_ORDER", entity_type="intent", entity_id=str(intent_id),
                  payload={"event": "changed", "raw": e})

    # ── 辅助 ────────────────────────────────────────────────────

    def filled_quantity(self, intent_id: int) -> float:
        return dbm.filled_quantity(self.conn, intent_id)

    def _upsert_broker_order(self, intent_id: int, broker_order_id: Optional[str], event: Dict) -> None:
        if not broker_order_id:
            return
        dbm.upsert_broker_order(self.conn, intent_id, broker_order_id, event)


class Reconciliation:
    """broker/local 对账。不一致 → fail closed（AccountState = MISMATCH）。"""

    def __init__(self, conn, broker=None):
        self.conn = conn
        self.broker = broker  # broker 需提供 order_state(broker_order_id) -> dict 或 positions()

    def reconcile_plan(self, plan_id: str) -> Dict:
        intents = dbm.list_intents(self.conn, plan_id)
        plan = dbm.get_plan(self.conn, plan_id)
        account_id = plan["account_id"] if plan is not None else "default"
        mismatches: List[str] = []
        for it in intents:
            broker_order_id = it["broker_order_id"] or ""
            if self.broker is None:
                if it["status"] in ("SUBMITTING", "SUBMITTED", "UNKNOWN"):
                    mismatches.append(f"{it['client_request_id']} 本地 {it['status']} 但无 broker 可查")
                continue
            if not broker_order_id:
                if it["status"] != "PENDING":
                    mismatches.append(f"{it['client_request_id']} 缺 broker_order_id")
                    dbm.set_intent_status(self.conn, it["intent_id"], "UNKNOWN")
                continue
            bo = self.broker.order_state(broker_order_id)
            if bo is None:
                mismatches.append(f"{it['client_request_id']} broker 无此订单")
                dbm.set_intent_status(self.conn, it["intent_id"], "UNKNOWN")
                continue
            normalized = normalize_broker_status(bo.get("status"))
            if normalized != it["status"] and not (
                    it["status"] == "SUBMITTING" and normalized == "SUBMITTED"):
                mismatches.append(
                    f"{it['client_request_id']} local={it['status']} broker={normalized}")
        ok = not mismatches
        if not ok:
            dbm.set_account_sync_status(self.conn, account_id, "MISMATCH")
        dbm.audit(self.conn, "RECONCILE", entity_type="plan", entity_id=plan_id,
                  payload={"ok": ok, "mismatches": mismatches})
        return {"ok": ok, "mismatches": mismatches}

    def reconcile_all(self) -> Dict:
        """批量对账所有含非终态订单的计划；任一失败即整体失败。"""
        plan_ids = dbm.plan_ids_for_reconciliation(self.conn)
        results = {plan_id: self.reconcile_plan(plan_id) for plan_id in plan_ids}
        mismatches = [
            f"{plan_id}: {message}"
            for plan_id, result in results.items()
            for message in result["mismatches"]
        ]
        return {
            "ok": not mismatches,
            "plans_checked": len(plan_ids),
            "mismatches": mismatches,
            "results": results,
        }


# ────────────────────────────────────────────────────────────────
# 冒烟测试
# ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    conn = dbm.get_conn(":memory:")
    eh = BrokerEventHandler(conn)
    from shared import db as dbm
    # 造 intent
    dbm.insert_intent(conn, "cr_test_1", "p1", "1", "NVDA.US", "BUY", 10,
                      reference_price=223.96)
    intent = dbm.list_intents(conn, "p1")[0]
    iid = intent["intent_id"]

    # submitted
    eh.handle({"type": "submitted", "intent_id": iid, "broker_order_id": "bo1"})
    assert dbm.get_intent(conn, iid)["status"] == "SUBMITTED"
    # partial fill 6/10
    eh.handle({"type": "filled", "intent_id": iid, "broker_order_id": "bo1",
               "symbol": "NVDA.US", "side": "BUY", "quantity": 6, "price": 224.0})
    assert dbm.get_intent(conn, iid)["status"] == "SUBMITTED", "partial fill 应保持 SUBMITTED"
    assert eh.filled_quantity(iid) == 6
    # 剩余 4/10 → FILLED
    eh.handle({"type": "filled", "intent_id": iid, "broker_order_id": "bo1",
               "symbol": "NVDA.US", "side": "BUY", "quantity": 4, "price": 224.2})
    assert dbm.get_intent(conn, iid)["status"] == "FILLED"
    assert eh.filled_quantity(iid) == 10

    # rejected
    dbm.insert_intent(conn, "cr_test_2", "p1", "2", "KO.US", "BUY", 20)
    iid2 = dbm.list_intents(conn, "p1")[1]["intent_id"]
    eh.handle({"type": "rejected", "intent_id": iid2, "reason": "no_shares"})
    assert dbm.get_intent(conn, iid2)["status"] == "REJECTED"

    # reconcile：本地 SUBMITTED 但无 broker → fail closed
    dbm.insert_intent(conn, "cr_test_3", "p2", "1", "SCO.US", "BUY", 80)
    iid3 = dbm.list_intents(conn, "p2")[0]["intent_id"]
    eh.handle({"type": "submitted", "intent_id": iid3, "broker_order_id": "bo3"})
    rec = Reconciliation(conn)  # 无 broker
    r = rec.reconcile_plan("p2")
    assert not r["ok"]
    acc = dbm.get_account(conn, "default")
    assert acc is not None and acc["sync_status"] == "MISMATCH", "不一致应 fail closed → MISMATCH"
    print("broker.py 冒烟测试通过 ✅")
