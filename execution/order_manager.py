#!/usr/bin/env python3
"""
ConfirmationService + OrderManager — 交易系统 v4.0（架构 D-3 / D-9 / D-12）
========================================================================
安全链核心：

    ApprovalAdapter（唯一能产生 APPROVED）→ ConfirmationService → OrderManager（原子消费）

- ApprovalAdapter：真实用户动作 → approve()。AI 可以生成 ExecutionPlan，AI 不能生成
  HumanConfirmation(APPROVED)（D-12）。approval_nonce 防 replay。
- OrderManager 原子消费不变量（D-3 多订单版）：一个 Plan 的**全部** OrderIntent 创建 +
  Confirmation 置 CONSUMED 在**同一个本地 DB 事务**内完成，全成或全败。
- 幂等（D-9）：plan_id + plan_order_id UNIQUE + client_request_id UNIQUE；
  重复执行同一 Plan 不重复创建任何 OrderIntent。
- 崩溃恢复：事务提交后崩溃 → recover() 恢复同一批 OrderIntent → reconcile → retry if safe，
  绝不重新创建。
"""

import uuid
from typing import List, Optional
from dataclasses import dataclass

from execution.models import (
    ExecutionPlan, Confirmation, PlanOrder, now_utc, compute_plan_hash, parse_ts,
    CONFIRMATION_STATUSES,
)
from shared import db as dbm


# ────────────────────────────────────────────────────────────────
# ApprovalAdapter（D-12）
# ────────────────────────────────────────────────────────────────

class ApprovalAdapter:
    """真实用户动作 → 生成 APPROVED Confirmation 的唯一路径。

    生产实现（CLI/Web/微信）必须验证：用户身份 + approval_nonce + plan_id + plan_hash + 防 replay。
    AI Agent 不持有本类的批准能力——由外部真实用户动作显式调用 approve()。
    """

    def __init__(self, conn, channel: str = "cli"):
        self.conn = conn
        self.channel = channel

    def approve(self, confirmation_id: str, approved_by: str, nonce: Optional[str] = None) -> Confirmation:
        """用户显式批准。nonce 必填（防 replay：UNIQUE 约束兜底）。"""
        nonce = nonce or uuid.uuid4().hex
        row = dbm.approve_confirmation(
            self.conn, confirmation_id, approved_by, self.channel, nonce)
        return Confirmation(**dict(row))


# ────────────────────────────────────────────────────────────────
# ConfirmationService（票据生命周期）
# ────────────────────────────────────────────────────────────────

class ConfirmationService:
    def __init__(self, core_conn, execution_conn=None):
        """创建 executiond-owned Confirmation。

        ``execution_conn`` 省略时保留 v4/DRY_RUN 单库兼容；提供时从 core_conn
        校验计划并把规范快照复制到 execution_conn，之后票据只写执行库。
        """
        self.core_conn = core_conn
        self.conn = execution_conn if execution_conn is not None else core_conn

    def create(self, plan: ExecutionPlan, expires_at: Optional[str] = None,
               confirmation_id: Optional[str] = None) -> Confirmation:
        """为不可变 Plan 创建 PENDING Confirmation（票据）。"""
        cid = confirmation_id or f"cfm_{uuid.uuid4().hex[:12]}"
        exp = expires_at or "2099-12-31T23:59:59Z"
        core_stored = dbm.get_plan(self.core_conn, plan.plan_id)
        if core_stored is not None and (
                core_stored["plan_hash"] != plan.plan_hash
                or core_stored["account_id"] != plan.account_id
                or core_stored["execution_mode"] != plan.execution_mode
                or core_stored["expires_at"] != plan.expires_at):
            raise ValueError(f"Core plan_id {plan.plan_id} 内容/hash 不匹配")
        if core_stored is None:
            # 仅为旧测试和 DRY_RUN API 保留；LIVE CLI 会在进入本服务前强制双库。
            dbm.insert_plan(self.core_conn, plan.plan_id, plan.account_id,
                            plan.execution_mode, plan.expires_at, plan.plan_hash,
                            [order.to_dict() for order in plan.orders])

        stored = dbm.get_plan(self.conn, plan.plan_id)
        if stored is None:
            dbm.insert_plan(self.conn, plan.plan_id, plan.account_id, plan.execution_mode,
                            plan.expires_at, plan.plan_hash,
                            [order.to_dict() for order in plan.orders])
        elif stored["plan_hash"] != plan.plan_hash:
            raise ValueError(f"plan_id {plan.plan_id} 已绑定其他内容")
        dbm.insert_confirmation(self.conn, cid, plan.plan_id, plan.plan_hash,
                                expires_at=exp, status="PENDING")
        row = dbm.get_confirmation(self.conn, cid)
        assert row is not None
        return Confirmation(**dict(row))

    def get(self, confirmation_id: str) -> Optional[Confirmation]:
        row = dbm.get_confirmation(self.conn, confirmation_id)
        return Confirmation(**dict(row)) if row else None


# ────────────────────────────────────────────────────────────────
# OrderManager（原子消费 + 幂等 + 崩溃恢复）
# ────────────────────────────────────────────────────────────────

class OrderManager:
    def __init__(self, conn, broker=None):
        self.conn = conn
        self.broker = broker  # 可选：真实券商客户端；None = dry-run（不实际下单）

    # ── 原子消费（D-3 核心不变量） ──────────────────────────────

    def consume(self, plan: ExecutionPlan, confirmation: Confirmation) -> List[dict]:
        """在一个 DB 事务内：创建 Plan 全部 OrderIntent + Confirmation 置 CONSUMED。

        全成或全败（任一失败 → ROLLBACK）。
        幂等：该 plan 已有 intents 时直接返回已有（绝不重复创建）。
        返回创建的 OrderIntent 列表（dict）。
        """
        current_hash = compute_plan_hash(
            plan.account_id, plan.execution_mode,
            [o.to_dict() for o in sorted(plan.orders, key=lambda x: x.plan_order_id)],
            plan.expires_at,
        )
        if current_hash != plan.plan_hash:
            raise RuntimeError("ExecutionPlan 内容在生成 hash 后被修改")

        # 幂等返回前仍验证票据属于当前计划，不能把另一计划的确认当作重试凭据。
        existing = dbm.list_intents(self.conn, plan.plan_id)
        if existing:
            row = dbm.get_confirmation(self.conn, confirmation.confirmation_id)
            if (row is None or row["status"] != "CONSUMED"
                    or row["plan_id"] != plan.plan_id or row["plan_hash"] != plan.plan_hash):
                raise RuntimeError("已有 intents 与当前 Confirmation/Plan 不匹配")
            return [dict(r) for r in existing]

        if confirmation.status != "APPROVED":
            # 显式抛 AssertionError，兼容原有契约且不会被 python -O 移除。
            raise AssertionError("只有 APPROVED 的 Confirmation 可消费")
        with dbm.immediate_transaction(self.conn):
            # 重新读 confirmation（锁行防并发双消费）
            row = dbm.approved_confirmation(self.conn, confirmation.confirmation_id)
            if row is None:
                raise RuntimeError(f"confirmation {confirmation.confirmation_id} 已消费或状态不对")
            if row["plan_id"] != plan.plan_id or confirmation.plan_id != plan.plan_id:
                raise RuntimeError("confirmation.plan_id 与 ExecutionPlan 不匹配")
            if row["plan_hash"] != plan.plan_hash:
                raise RuntimeError("plan_hash 不匹配：计划已被修改，Confirmation 失效")
            if row["expires_at"] and parse_ts(row["expires_at"]) < parse_ts(now_utc()):
                raise RuntimeError("confirmation 已过期")
            if plan.is_expired():
                raise RuntimeError("plan 已过期")

            # 事务内二次幂等检查（并发场景兜底）
            if dbm.list_intents(self.conn, plan.plan_id):
                return [dict(r) for r in dbm.list_intents(self.conn, plan.plan_id)]

            created = []
            for o in sorted(plan.orders, key=lambda x: x.plan_order_id):
                client_request_id = f"cr_{plan.plan_id}_{o.plan_order_id}"
                dbm.insert_intent(
                    self.conn, client_request_id, plan.plan_id, o.plan_order_id,
                    o.symbol, o.side, o.quantity, o.order_type,
                    reference_price=o.reference_price, max_slippage_bps=o.max_slippage_bps,
                    status="PENDING", strategy_version_id=o.strategy_version_id,
                    confirmation_id=confirmation.confirmation_id)
                created.append({"client_request_id": client_request_id,
                                "plan_id": plan.plan_id, "plan_order_id": o.plan_order_id,
                                "symbol": o.symbol, "side": o.side, "quantity": o.quantity,
                                "order_type": o.order_type,
                                "reference_price": o.reference_price,
                                "max_slippage_bps": o.max_slippage_bps,
                                "strategy_version_id": o.strategy_version_id,
                                "confirmation_id": confirmation.confirmation_id,
                                "status": "PENDING"})

            # Confirmation 置 CONSUMED（同事务）
            dbm.mark_plan_consumed(
                self.conn, confirmation.confirmation_id, plan.plan_id)
            dbm.audit(self.conn, "ORDER_INTENT", entity_type="plan", entity_id=plan.plan_id,
                      payload={"created": [c["client_request_id"] for c in created],
                               "confirmation": confirmation.confirmation_id,
                               "execution_mode": plan.execution_mode}, commit=False)
        from production.notification import safe_notify
        notification_adapter = None
        if plan.execution_mode == "LIVE":
            # consume 位于券商提交前，LIVE 关键路径只写本地审计，不等待网络 webhook。
            from production.notification import AuditNotificationAdapter
            notification_adapter = AuditNotificationAdapter(self.conn)
        safe_notify(
            self.conn, "order_intent.created",
            f"{plan.plan_id} 已消费确认票据",
            f"created={len(created)}, mode={plan.execution_mode}",
            severity="WARNING" if plan.execution_mode == "LIVE" else "INFO",
            entity_type="plan", entity_id=plan.plan_id,
            adapter=notification_adapter,
        )
        return created

    # ── 崩溃恢复（D-3） ─────────────────────────────────────────

    def recover(self, plan_id: str) -> List[dict]:
        """事务提交后崩溃 → 恢复同一批 OrderIntent。

        幂等：intents 已存在就直接返回（绝不重新创建）。
        """
        existing = dbm.list_intents(self.conn, plan_id)
        if existing:
            return [dict(r) for r in existing]
        return []

    def submit(self, plan: ExecutionPlan, confirmation: Confirmation,
               market_states: Optional[dict] = None, account_state=None) -> List[dict]:
        """安全提交入口：consume（原子事务）→ 按 execution_mode 路由。

        - DRY_RUN：创建 intents 后即止（不调券商，系统默认模式）
        - LIVE：必须 broker 可用，且仅限已通过 PreTradeRisk 的调用方
        """
        if plan.execution_mode == "LIVE":
            from execution.pretrade_risk import evaluate
            all_intents = dbm.list_intents(self.conn)
            risk = evaluate(
                plan, confirmation, account_state, market_states or {},
                pending_intents=sum(1 for row in all_intents
                                    if row["status"] in ("PENDING", "SUBMITTING", "SUBMITTED")),
                unknown_intents=sum(1 for row in all_intents if row["status"] == "UNKNOWN"),
            )
            dbm.audit(self.conn, "PRETRADE", entity_type="plan", entity_id=plan.plan_id,
                      payload={"decision": risk.decision, "reasons": risk.reasons})
            if not risk.passed:
                if any("UNKNOWN" in reason or "MISMATCH" in reason
                       for reason in risk.reasons):
                    dbm.close_live_canaries(
                        self.conn, "pretrade_unknown_or_mismatch", plan.account_id)
                from production.notification import safe_notify
                safe_notify(
                    self.conn, "pretrade.rejected", f"{plan.plan_id} 风控拒绝",
                    "; ".join(risk.reasons), severity="ERROR",
                    entity_type="plan", entity_id=plan.plan_id,
                )
                raise RuntimeError("PreTradeRisk REJECT: " + "; ".join(risk.reasons))

        created = self.consume(plan, confirmation)
        if plan.execution_mode == "LIVE":
            if self.broker is None:
                raise RuntimeError("LIVE 模式需要 broker 客户端，当前未配置（保持 dry-run）")
            for intent in created:
                stored = dbm.get_intent_by_request_id(self.conn, intent["client_request_id"])
                if stored is None or stored["status"] != "PENDING":
                    continue
                dbm.set_intent_status(self.conn, stored["intent_id"], "SUBMITTING")
                # US-007：确认 + 计划随链传入 broker（LiveBroker.submit 内部再断言
                # confirmation 已 APPROVED 且已 CONSUMED，杜绝绕过确认的直通提交）
                try:
                    ack = self.broker.submit_order(dict(stored), confirmation=confirmation, plan=plan)
                except Exception:
                    dbm.set_intent_status(self.conn, stored["intent_id"], "UNKNOWN")
                    dbm.close_live_canaries(
                        self.conn, "broker_submit_unknown_or_credential_error",
                        plan.account_id)
                    raise
                dbm.set_intent_status(self.conn, stored["intent_id"], "SUBMITTED",
                                      ack.broker_order_id)
                # SDK 回调线程只负责入队；ack 落库后由当前线程安全消费。
                if hasattr(self.broker, "drain_events"):
                    self.broker.drain_events()
            from production.notification import safe_notify
            safe_notify(
                self.conn, "broker.submitted", f"{plan.plan_id} 已提交券商",
                f"PreTradeRisk=PASS, intents={len(created)}", severity="WARNING",
                entity_type="plan", entity_id=plan.plan_id,
            )
        return created


# ────────────────────────────────────────────────────────────────
# 冒烟测试
# ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    conn = dbm.get_conn(":memory:")
    svc = ConfirmationService(conn)
    adapter = ApprovalAdapter(conn, channel="cli")
    om = OrderManager(conn)

    plan = ExecutionPlan(
        plan_id="p1", account_id="default", execution_mode="DRY_RUN",
        expires_at="2099-01-01T00:00:00Z",
        orders=[PlanOrder("1", "NVDA.US", "BUY", 10, reference_price=223.96),
                PlanOrder("2", "KO.US", "BUY", 20, reference_price=86.83)])
    cfm = svc.create(plan)
    approved = adapter.approve(cfm.confirmation_id, approved_by="owner", nonce="n1")
    assert approved.status == "APPROVED"
    created = om.consume(plan, approved)
    assert len(created) == 2
    # 幂等：重复消费 → 返回已有，不重复创建
    created2 = om.consume(plan, approved)
    assert len(created2) == 2
    intents = dbm.list_intents(conn, "p1")
    assert len(intents) == 2, "幂等失败：重复执行不应创建新 intent"
    # 崩溃恢复
    recovered = om.recover("p1")
    assert len(recovered) == 2
    # plan_hash 不匹配 → 拒绝（新 plan + 被篡改 plan_hash 的 confirmation）
    plan2 = ExecutionPlan(
        plan_id="p2", account_id="default", execution_mode="DRY_RUN",
        expires_at="2099-01-01T00:00:00Z",
        orders=[PlanOrder("1", "NVDA.US", "BUY", 10, reference_price=223.96)])
    tampered = Confirmation(confirmation_id="cfm_bad", plan_id="p2",
                            plan_hash="deadbeef", status="APPROVED")
    try:
        om.consume(plan2, tampered)
        raise AssertionError("plan_hash 不匹配应拒绝")
    except RuntimeError:
        pass
    print("order_manager.py 冒烟测试通过 ✅")
