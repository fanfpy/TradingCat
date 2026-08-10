#!/usr/bin/env python3
"""v5 executiond 应用服务：Core 只读计划，Execution 独占审批与订单状态。

外部调用者只能提交 ``plan_id`` 与认证后的 ``ApprovalProof``，不能携带临时订单
覆盖计划。服务从 Core store 读取不可变计划、独立重算 hash，并把规范化快照复制
到 execution store。Confirmation 只在 execution store 中创建和消费。
"""

import json
import uuid
from typing import Optional

from execution.models import Confirmation, ExecutionPlan, PlanOrder
from execution.approval_wechat import IdentityProof, IdentityVerifier
from shared import db as dbm


# 对外采用架构 v5 名称；保留 IdentityProof 的 wire shape 兼容已有微信适配器。
ApprovalProof = IdentityProof


class ExecutionService:
    """独立 executiond 的最小可信边界。"""

    def __init__(self, core_conn, execution_conn, *,
                 identity_verifier: Optional[IdentityVerifier] = None,
                 require_separate: bool = True):
        if require_separate:
            dbm.assert_separate_stores(core_conn, execution_conn)
        self.core_conn = core_conn
        self.execution_conn = execution_conn
        self.identity_verifier = identity_verifier

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

        dbm.insert_plan(
            self.execution_conn, plan.plan_id, plan.account_id,
            plan.execution_mode, plan.expires_at, plan.plan_hash,
            [order.to_dict() for order in plan.orders],
        )
        return plan

    def request_confirmation(self, plan_id: str, *,
                             expires_at: Optional[str] = None,
                             confirmation_id: Optional[str] = None) -> Confirmation:
        """在 execution store 创建 PENDING Confirmation。"""
        plan = self.read_and_snapshot_plan(plan_id)
        cid = confirmation_id or f"cfm_{uuid.uuid4().hex[:12]}"
        exp = expires_at or "2099-12-31T23:59:59Z"
        dbm.insert_confirmation(
            self.execution_conn, cid, plan.plan_id, plan.plan_hash,
            expires_at=exp, status="PENDING",
        )
        row = dbm.get_confirmation(self.execution_conn, cid)
        assert row is not None
        return Confirmation(**dict(row))

    def approve(self, confirmation_id: str, proof: ApprovalProof) -> Confirmation:
        """验证 ApprovalProof 后才在 execution store 写入 APPROVED。

        Proof 签名覆盖 subject/action/confirmation id/plan id/plan hash/nonce/timestamp；
        verifier 返回经过身份映射的 owner，调用方提供的 approved_by 不被信任。
        """
        if self.identity_verifier is None:
            raise RuntimeError("executiond 未配置 ApprovalProof verifier")
        row = dbm.get_confirmation(self.execution_conn, confirmation_id)
        if row is None:
            raise ValueError(f"execution confirmation 不存在: {confirmation_id}")
        owner = self.identity_verifier.verify(
            proof, action="approve", confirmation_id=confirmation_id,
            plan_id=row["plan_id"], plan_hash=row["plan_hash"],
        )
        approved = dbm.approve_confirmation(
            self.execution_conn, confirmation_id, owner, "approval-proof",
            proof.nonce, expected_plan_id=row["plan_id"],
            expected_plan_hash=row["plan_hash"],
        )
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
