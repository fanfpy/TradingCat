#!/usr/bin/env python3
"""v5 executiond 应用服务：Core 只读计划，Execution 独占审批与订单状态。

外部调用者只能提交 ``plan_id`` 与认证后的 ``ApprovalProof``，不能携带临时订单
覆盖计划。服务从 Core store 读取不可变计划、独立重算 hash，并把规范化快照复制
到 execution store。Confirmation 只在 execution store 中创建和消费。
"""

import json
import sqlite3
import uuid
from typing import Optional

from execution.models import (
    APPROVAL_PROOF_CHANNEL, Confirmation, ExecutionPlan, MarketState, PlanOrder,
    parse_ts,
)
from execution.approval_wechat import IdentityProof, IdentityVerifier
from execution.broker_live import LiveBroker, UnknownOutcomeError
from execution.broker import Reconciliation
from execution.order_manager import OrderManager
from execution.paper_broker import PaperBroker
from shared.account import load as load_account_state
from shared import db as dbm
from execution.persistence import insert_plan


# 对外采用架构 v5 名称；保留 IdentityProof 的 wire shape 兼容已有微信适配器。
ApprovalProof = IdentityProof


class ApprovalServiceError(ValueError):
    """稳定地映射到 canonical approve JSON 的业务错误码。"""

    error_code = "APPROVAL_REJECTED"


class ConfirmationNotFoundError(ApprovalServiceError):
    error_code = "CONFIRMATION_NOT_FOUND"


class ConfirmationExpiredError(ApprovalServiceError):
    error_code = "CONFIRMATION_EXPIRED"


class ConfirmationNotPendingError(ApprovalServiceError):
    error_code = "CONFIRMATION_NOT_PENDING"


class PlanNotFoundError(ApprovalServiceError):
    error_code = "PLAN_NOT_FOUND"


class PlanExpiredError(ApprovalServiceError):
    error_code = "PLAN_EXPIRED"


class PlanHashMismatchError(ApprovalServiceError):
    error_code = "PLAN_HASH_MISMATCH"


class ApprovalProofClaimMismatchError(ApprovalServiceError):
    error_code = "APPROVAL_PROOF_CLAIM_MISMATCH"


class ApprovalNonceReplayError(ApprovalServiceError):
    error_code = "APPROVAL_NONCE_REPLAY"


class ExecutionService:
    """独立 executiond 的最小可信边界。"""

    def __init__(self, core_conn, execution_conn, *,
                 identity_verifier: Optional[IdentityVerifier] = None,
                 require_separate: bool = True,
                 broker=None, risk_limits=None, daily_loss=None):
        if require_separate:
            dbm.assert_separate_stores(core_conn, execution_conn)
        self.core_conn = core_conn
        self.execution_conn = execution_conn
        self.identity_verifier = identity_verifier
        self.broker = broker
        self.risk_limits = risk_limits
        self.daily_loss = daily_loss

    def read_and_snapshot_plan(self, plan_id: str) -> ExecutionPlan:
        """按 id 读取 Core 计划，重新规范化并计算 hash，再复制到执行库。"""
        row = dbm.get_plan(self.core_conn, plan_id)
        if row is None:
            raise ValueError(f"Core ExecutionPlan 不存在: {plan_id}")
        try:
            orders = tuple(PlanOrder(**item) for item in json.loads(row["orders_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"Core ExecutionPlan 内容无法规范化: {plan_id}") from exc
        plan = ExecutionPlan(
            plan_id=row["plan_id"], account_id=row["account_id"],
            execution_mode=row["execution_mode"], expires_at=row["expires_at"],
            orders=orders,
        )
        if plan.plan_hash != row["plan_hash"]:
            raise RuntimeError("Core ExecutionPlan hash 校验失败，拒绝建立执行快照")

        insert_plan(
            self.execution_conn, plan.plan_id, plan.account_id,
            plan.execution_mode, plan.expires_at, plan.plan_hash,
            [order.to_dict() for order in plan.orders],
        )
        return plan

    def request_confirmation(self, plan_id: str, *,
                             plan_hash: Optional[str] = None,
                             idempotency_key: Optional[str] = None,
                             expires_at: Optional[str] = None,
                             confirmation_id: Optional[str] = None) -> Confirmation:
        """在 execution store 创建 PENDING Confirmation。"""
        plan = self.read_and_snapshot_plan(plan_id)
        if plan_hash is not None and plan_hash != plan.plan_hash:
            raise ValueError("plan_hash 与不可变计划不匹配")
        if idempotency_key:
            existing = dbm.get_confirmation_by_idempotency_key(
                self.execution_conn, idempotency_key)
            if existing is not None:
                if (existing["plan_id"] != plan.plan_id
                        or existing["plan_hash"] != plan.plan_hash):
                    raise ValueError("idempotency_key 已绑定其他计划")
                return Confirmation(**dict(existing))
        cid = confirmation_id or f"cfm_{uuid.uuid4().hex[:12]}"
        exp = expires_at or plan.expires_at
        if parse_ts(exp) > parse_ts(plan.expires_at):
            exp = plan.expires_at
        try:
            dbm.insert_confirmation(
                self.execution_conn, cid, plan.plan_id, plan.plan_hash,
                expires_at=exp, status="PENDING", idempotency_key=idempotency_key,
            )
        except sqlite3.IntegrityError:
            # A concurrent retry may win the unique idempotency-key insert.
            # Return that same durable confirmation after re-checking its binding.
            if not idempotency_key:
                raise
            existing = dbm.get_confirmation_by_idempotency_key(
                self.execution_conn, idempotency_key)
            if existing is None:
                raise
            if (existing["plan_id"] != plan.plan_id
                    or existing["plan_hash"] != plan.plan_hash):
                raise ValueError("idempotency_key 已绑定其他计划")
            return Confirmation(**dict(existing))
        row = dbm.get_confirmation(self.execution_conn, cid)
        assert row is not None
        return Confirmation(**dict(row))

    def approve(self, confirmation_id: str, proof: ApprovalProof) -> Confirmation:
        """验证 ApprovalProof 后才在 execution store 写入 APPROVED。

        Proof 签名覆盖 subject/action/confirmation id/plan id/plan hash/nonce/timestamp；
        verifier 返回经过身份映射的 owner，调用方提供的 approved_by 不被信任。
        """
        if self.identity_verifier is None:
            error = ApprovalServiceError("executiond 未配置 ApprovalProof verifier")
            error.error_code = "APPROVAL_VERIFIER_UNAVAILABLE"
            raise error
        row = dbm.get_confirmation(self.execution_conn, confirmation_id)
        if row is None:
            raise ConfirmationNotFoundError(
                f"execution confirmation 不存在: {confirmation_id}")
        confirmation = Confirmation(**dict(row))
        if confirmation.status != "PENDING":
            raise ConfirmationNotPendingError(
                f"confirmation 状态 {confirmation.status} 不可批准（只允许 PENDING）")

        snapshot = self.get_snapshot(row["plan_id"])
        if snapshot is None:
            raise PlanNotFoundError(f"execution plan 不存在: {row['plan_id']}")
        if snapshot.plan_hash != row["plan_hash"]:
            raise PlanHashMismatchError("confirmation.plan_hash 与 execution plan 不匹配")
        if snapshot.is_expired():
            raise PlanExpiredError(f"plan 已过期: {snapshot.plan_id}")
        if confirmation.is_expired():
            raise ConfirmationExpiredError(
                f"confirmation 已过期: {confirmation_id}")

        # Canonical proofs carry all claims.  Legacy callers may omit the
        # optional fields, but any supplied claim must still match the stored
        # immutable confirmation before the verifier is called.
        for field, expected in (("action", "approve"),
                                ("confirmation_id", confirmation_id),
                                ("plan_id", row["plan_id"]),
                                ("plan_hash", row["plan_hash"])):
            actual = getattr(proof, field, None)
            if actual is not None and actual != expected:
                raise ApprovalProofClaimMismatchError(
                    f"ApprovalProof {field} 与 confirmation 不匹配")
        owner = self.identity_verifier.verify(
            proof, action="approve", confirmation_id=confirmation_id,
            plan_id=row["plan_id"], plan_hash=row["plan_hash"],
        )
        try:
            approved = dbm.approve_confirmation(
                self.execution_conn, confirmation_id, owner, APPROVAL_PROOF_CHANNEL,
                proof.nonce, expected_plan_id=row["plan_id"],
                expected_plan_hash=row["plan_hash"],
            )
        except ValueError as exc:
            if "approval_nonce 已使用" in str(exc):
                raise ApprovalNonceReplayError(str(exc)) from exc
            raise
        return Confirmation(**dict(approved))

    def reject(self, confirmation_id: str, proof: ApprovalProof,
               reason: str = "") -> Confirmation:
        if self.identity_verifier is None:
            raise RuntimeError("executiond 未配置 ApprovalProof verifier")
        row = dbm.get_confirmation(self.execution_conn, confirmation_id)
        if row is None:
            raise ValueError(f"execution confirmation 不存在: {confirmation_id}")
        owner = self.identity_verifier.verify(
            proof, action="reject", confirmation_id=confirmation_id,
            plan_id=row["plan_id"], plan_hash=row["plan_hash"],
        )
        rejected = dbm.reject_confirmation(
            self.execution_conn, confirmation_id, owner, "approval-proof",
            proof.nonce, reason,
        )
        return Confirmation(**dict(rejected))

    def get_snapshot(self, plan_id: str) -> Optional[ExecutionPlan]:
        """读取执行库中的规范化计划快照，并再次验证 hash。"""
        row = dbm.get_plan(self.execution_conn, plan_id)
        if row is None:
            return None
        orders = tuple(PlanOrder(**item) for item in json.loads(row["orders_json"]))
        plan = ExecutionPlan(row["plan_id"], row["account_id"],
                             row["execution_mode"], row["expires_at"], orders)
        if plan.plan_hash != row["plan_hash"]:
            raise RuntimeError("Canonical Plan Snapshot hash 校验失败")
        return plan

    def _load_risk_inputs(self, plan: ExecutionPlan):
        """从本地 Core 快照加载风控输入；绝不触发券商同步或网络访问。"""
        account = load_account_state(self.core_conn, plan.account_id)
        states = {}
        for order in plan.orders:
            row = dbm.get_market_state(self.core_conn, order.symbol)
            if row is not None:
                states[order.symbol] = MarketState(
                    row["symbol"], row["quote_at"], float(row["price"]),
                    int(row["max_age_seconds"]),
                )
        return account, states

    def _live_deployment_ready(self) -> bool:
        row = self.execution_conn.execute(
            "SELECT status FROM system_readiness WHERE gate='P0_A'"
        ).fetchone()
        return row is not None and row["status"] == "PASS"

    def health(self) -> dict:
        """无副作用的 executiond 健康检查；不读取凭证、不连接券商。"""
        try:
            self.execution_conn.execute("SELECT 1").fetchone()
            execution_store = "READY"
        except Exception:
            execution_store = "UNAVAILABLE"
        try:
            self.core_conn.execute("SELECT 1").fetchone()
            core_store = "READY"
        except Exception:
            core_store = "UNAVAILABLE"
        return {
            "status": "OK" if execution_store == core_store == "READY" else "DEGRADED",
            "execution_store": execution_store,
            "core_store": core_store,
            "live_submit_rpc": False,
            "operations": ("request_confirmation", "approve", "reject", "execute",
                           "execute_status", "readiness", "reconcile_status", "reconcile"),
        }

    def readiness(self) -> dict:
        """返回 LIVE 前置门状态；该 RPC 不能修改 readiness 或 Canary。"""
        row = self.execution_conn.execute(
            "SELECT status, evidence_hash, accepted_at FROM system_readiness WHERE gate='P0_A'"
        ).fetchone()
        canaries = self.execution_conn.execute(
            "SELECT COUNT(*) AS count FROM live_canary WHERE status='ACTIVE'"
        ).fetchone()["count"]
        return {
            "status": "READY" if row is not None and row["status"] == "PASS" else "NOT_READY",
            "p0_a": dict(row) if row is not None else {"status": "MISSING"},
            "active_canaries": canaries,
            "live_requirements": ("P0_A=PASS", "ACTIVE_CANARY", "ApprovalProof",
                                  "PreTradeRisk=PASS"),
        }

    def execute_status(self, *, plan_id: str, confirmation_id: str) -> dict:
        """只按不可变标识符查询执行状态，不接受订单字段或重试指令。"""
        plan = self.get_snapshot(plan_id)
        if plan is None:
            raise PlanNotFoundError(f"execution plan 不存在: {plan_id}")
        confirmation = dbm.get_confirmation(self.execution_conn, confirmation_id)
        if confirmation is None:
            raise ConfirmationNotFoundError(
                f"execution confirmation 不存在: {confirmation_id}")
        if (confirmation["plan_id"] != plan_id
                or confirmation["plan_hash"] != plan.plan_hash):
            raise PlanHashMismatchError("confirmation 与 execution plan 不匹配")
        intents = [dict(intent) for intent in dbm.list_intents(self.execution_conn, plan_id)]
        statuses = sorted({intent["status"] for intent in intents})
        return {
            "plan_id": plan_id,
            "confirmation_id": confirmation_id,
            "mode": plan.execution_mode,
            "confirmation_status": confirmation["status"],
            "intent_count": len(intents),
            "intent_statuses": statuses,
            "unknown_outcome": any(status == "UNKNOWN" for status in statuses),
            "retry": False,
        }

    def reconcile_status(self, *, plan_id: str) -> dict:
        """读取最近一次对账审计；不触发 broker 查询。"""
        plan = self.get_snapshot(plan_id)
        if plan is None:
            raise PlanNotFoundError(f"execution plan 不存在: {plan_id}")
        audit = self.execution_conn.execute(
            "SELECT payload_json, ts FROM audit_log WHERE event='RECONCILE' "
            "AND entity_type='plan' AND entity_id=? ORDER BY id DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        payload = json.loads(audit["payload_json"]) if audit is not None else None
        return {
            "plan_id": plan_id,
            "mode": plan.execution_mode,
            "last_reconciliation": payload,
            "reconciled_at": audit["ts"] if audit is not None else None,
            "unknown_outcome": any(
                intent["status"] == "UNKNOWN"
                for intent in dbm.list_intents(self.execution_conn, plan_id)
            ),
        }

    def reconcile(self, *, plan_id: str) -> dict:
        """触发单计划对账。PAPER 本地检查；LIVE 只能用已配置 broker 的只读查询。"""
        plan = self.get_snapshot(plan_id)
        if plan is None:
            raise PlanNotFoundError(f"execution plan 不存在: {plan_id}")
        if plan.execution_mode == "LIVE":
            if not isinstance(self.broker, LiveBroker):
                raise RuntimeError("LIVE reconciliation 需要已配置 LiveBroker")
            if not self.broker.enable_order_queries:
                raise RuntimeError("LIVE reconciliation 需要显式 enable_order_queries=True")
        result = Reconciliation(self.core_conn, self.execution_conn, self.broker).reconcile_plan(plan_id)
        return {"plan_id": plan_id, "mode": plan.execution_mode, **result}

    def execute(self, *, plan_id: str, confirmation_id: str) -> dict:
        """Execute the one narrow RPC boundary.

        The caller supplies identifiers only.  The immutable plan, confirmation,
        account snapshot and quotes are read locally; no order fields are accepted
        or merged from the request.
        """
        if not isinstance(plan_id, str) or not plan_id:
            raise ValueError("execute.plan_id 必须是非空字符串")
        if not isinstance(confirmation_id, str) or not confirmation_id:
            raise ValueError("execute.confirmation_id 必须是非空字符串")

        plan = self.get_snapshot(plan_id)
        if plan is None:
            raise ValueError(f"execution plan 不存在: {plan_id}")
        row = dbm.get_confirmation(self.execution_conn, confirmation_id)
        if row is None:
            raise ValueError(f"execution confirmation 不存在: {confirmation_id}")
        if row["plan_id"] != plan.plan_id:
            raise RuntimeError("confirmation.plan_id 与 plan_id 不匹配")
        if row["plan_hash"] != plan.plan_hash:
            raise RuntimeError("confirmation.plan_hash 与 plan_hash 不匹配")
        if row["approval_channel"] != APPROVAL_PROOF_CHANNEL:
            raise RuntimeError("execute 只接受 approval-proof")
        unknown = next(
            (intent for intent in dbm.list_intents(self.execution_conn, plan.plan_id)
             if intent["status"] == "UNKNOWN"),
            None,
        )
        if unknown is not None:
            return {
                "status": "UNKNOWN_OUTCOME",
                "plan_id": plan.plan_id,
                "confirmation_id": confirmation_id,
                "message": "previous broker outcome is unknown; manual reconciliation required",
                "retry": False,
            }
        if row["status"] != "APPROVED":
            if row["status"] == "CONSUMED":
                raise RuntimeError("confirmation 已消费，禁止自动重试")
            raise RuntimeError(f"confirmation 状态 {row['status']} != APPROVED")

        confirmation = Confirmation(**dict(row))
        account_state, market_states = self._load_risk_inputs(plan)

        if plan.execution_mode == "PAPER":
            broker = self.broker or PaperBroker(self.execution_conn)
            if not isinstance(broker, PaperBroker):
                raise RuntimeError("PAPER 模式只允许本地 PaperBroker")
        elif plan.execution_mode == "LIVE":
            broker = self.broker
            if not isinstance(broker, LiveBroker) or not broker.enable_live:
                raise RuntimeError("LIVE 模式必须使用显式 enable_live=True 的 LiveBroker")
            if broker.kill_switch_engaged:
                raise RuntimeError("LIVE kill switch 已 engaged")
            if not self._live_deployment_ready():
                raise RuntimeError("LIVE 部署门禁未通过：需要 P0_A readiness=PASS")
        else:
            raise RuntimeError("execute 只接受 PAPER 或 LIVE 计划")

        manager = OrderManager(
            self.execution_conn, broker=broker,
            risk_limits=self.risk_limits, daily_loss=self.daily_loss,
        )
        try:
            intents = manager.submit(
                plan, confirmation, market_states=market_states,
                account_state=account_state,
            )
        except UnknownOutcomeError as exc:
            return {
                "status": "UNKNOWN_OUTCOME",
                "plan_id": plan.plan_id,
                "confirmation_id": confirmation.confirmation_id,
                "message": str(exc),
                "retry": False,
            }
        return {
            "status": "SUBMITTED",
            "plan_id": plan.plan_id,
            "confirmation_id": confirmation.confirmation_id,
            "mode": plan.execution_mode,
            "intents": intents,
        }
