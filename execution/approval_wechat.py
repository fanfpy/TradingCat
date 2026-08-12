#!/usr/bin/env python3
"""
Wechat 远程审批 transport — 交易系统 v4.0（US-006，P5 实现）
============================================================
在微信上审批交易计划：离开电脑也能走人工确认链（D-12 身份字段 + 接口约束 P0 写死）。

P5 定位声明（务必阅读）：
- 本模块是 **P5 核心审批 transport**：approval_channel=wechat + 防重放 +
  plan_hash 展示强绑定 + 远程 subject→owner 身份验证。
- ``approved_by`` 不再信任 transport 字符串：IdentityProof 的 HMAC 同时覆盖 subject、
  action、confirmation_id、plan_id、plan_hash、nonce、timestamp，并校验 owner 映射。
- 生产链路不变量不变：WechatApprovalAdapter（唯一能产生 APPROVED）→
  ConfirmationService → OrderManager（原子消费）。本 transport 只是
  ApprovalAdapter 的 wechat 通道实现，AI Agent 依然不能自行批准。

与 CLI transport（order_manager.ApprovalAdapter）的区别：
1. approve() 必须显式传入 plan + expected_plan_hash：
   - expected_plan_hash = 用户在微信上看到的展示摘要所对应的 plan_hash
     （微信卡片 / 审批链接展示的计划摘要）
   - 必须与 plan.plan_hash 完全一致，否则 PlanHashMismatchError（REJECT）
   —— 防"展示内容被篡改"的中间人风险：用户批准的东西必须与系统执行的东西一致。
2. 显式 approval_nonce 防重放：BEGIN IMMEDIATE 内检查 + 条件 UPDATE，DB 的
   UNIQUE(approval_nonce) 再兜底，关闭并发 TOCTOU 窗口。
3. reject() 显式拒绝动作 → status=REJECTED 落库 + audit lineage；与 approve
   一致做 nonce 唯一性校验（防重放，写库前校验避免 REPLACE 删除已有票据）。

用法：
    adapter = WechatApprovalAdapter(conn, channel="wechat")
    # identity_proof 必须由受信审批网关按 HMACIdentityVerifier.payload() 签名
    approved = adapter.approve(cfm.confirmation_id, approved_by="owner",
                               plan=plan, expected_plan_hash=plan.plan_hash,
                               nonce=nonce)
    rejected = adapter.reject(cfm.confirmation_id, approved_by="owner", reason="...")
"""

import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Optional, Protocol

from execution.models import ExecutionPlan, Confirmation
from execution.order_manager import ApprovalAdapter
from shared import db as dbm


class ReplayError(ValueError):
    """approval_nonce 已被使用（防重放拒绝）。"""

    error_code = "APPROVAL_NONCE_REPLAY"


class PlanHashMismatchError(ValueError):
    """expected_plan_hash 与 plan.plan_hash 不一致（展示值与执行计划强绑定失败）。"""

    error_code = "PLAN_HASH_MISMATCH"


class ApprovalIdentityError(ValueError):
    """远程用户身份签名无效、过期或未绑定 owner。"""

    error_code = "APPROVAL_PROOF_INVALID"


@dataclass(frozen=True)
class IdentityProof:
    subject: str
    timestamp: int
    nonce: str
    signature: str
    # These claims are optional for the legacy WeChat adapter wire shape.  The
    # canonical approve JSON contract requires them and the verifier binds
    # them to the request supplied by executiond.
    action: Optional[str] = None
    confirmation_id: Optional[str] = None
    plan_id: Optional[str] = None
    plan_hash: Optional[str] = None


class IdentityVerifier(Protocol):
    def verify(self, proof: IdentityProof, *, action: str, confirmation_id: str,
               plan_id: str, plan_hash: str) -> str:
        """验证后返回系统 owner 身份。"""
        ...


class HMACIdentityVerifier:
    """适用于微信网关/自建审批服务的 HMAC 身份验证器。"""

    def __init__(self, secret: str, owner_map: Dict[str, str],
                 max_age_seconds: int = 300):
        if len(secret) < 32:
            raise ApprovalIdentityError("审批身份密钥至少 32 字符")
        self.secret = secret.encode("utf-8")
        self.owner_map = dict(owner_map)
        self.max_age_seconds = max_age_seconds

    @classmethod
    def from_env(cls):
        from shared.env import load_selected
        load_selected(("TRADINGCAT_APPROVAL_IDENTITY_SECRET",
                       "TRADINGCAT_APPROVAL_OWNER_MAP"))
        secret = os.environ.get("TRADINGCAT_APPROVAL_IDENTITY_SECRET", "")
        raw_map = os.environ.get("TRADINGCAT_APPROVAL_OWNER_MAP", "")
        try:
            owner_map = json.loads(raw_map) if raw_map else {}
        except json.JSONDecodeError as exc:
            raise ApprovalIdentityError("TRADINGCAT_APPROVAL_OWNER_MAP 不是有效 JSON") from exc
        if not secret or not owner_map:
            raise ApprovalIdentityError(
                "远程审批未配置身份验证：需要 APPROVAL_IDENTITY_SECRET 和 OWNER_MAP")
        return cls(secret, owner_map)

    @staticmethod
    def payload(proof: IdentityProof, *, action: str, confirmation_id: str,
                plan_id: str, plan_hash: str) -> bytes:
        return "|".join((proof.subject, action, confirmation_id, plan_id,
                         plan_hash, proof.nonce, str(proof.timestamp))).encode("utf-8")

    def verify(self, proof: IdentityProof, *, action: str, confirmation_id: str,
               plan_id: str, plan_hash: str) -> str:
        for field, expected in (("action", action),
                                ("confirmation_id", confirmation_id),
                                ("plan_id", plan_id),
                                ("plan_hash", plan_hash)):
            actual = getattr(proof, field)
            if actual is not None and actual != expected:
                raise ApprovalIdentityError(
                    f"ApprovalProof {field} 与请求不匹配")
        if abs(int(time.time()) - int(proof.timestamp)) > self.max_age_seconds:
            raise ApprovalIdentityError("审批身份签名已过期")
        owner = self.owner_map.get(proof.subject)
        if not owner:
            raise ApprovalIdentityError(f"远程用户未绑定 owner: {proof.subject}")
        expected = hmac.new(
            self.secret,
            self.payload(proof, action=action, confirmation_id=confirmation_id,
                         plan_id=plan_id, plan_hash=plan_hash),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, proof.signature):
            raise ApprovalIdentityError("审批身份签名无效")
        return owner


class WechatApprovalAdapter(ApprovalAdapter):
    """微信远程审批 transport（P5：身份绑定 + 内容绑定 + 防重放）。

    继承 ApprovalAdapter（D-12）：唯一能产生 APPROVED Confirmation 的路径之一。
    相比 CLI transport 额外强制 plan_hash 展示值绑定 + 显式 nonce 防重放。
    """

    def __init__(self, conn, channel: str = "wechat",
                 identity_verifier: Optional[IdentityVerifier] = None):
        super().__init__(conn, channel=channel)
        self.identity_verifier = identity_verifier

    def _verify_identity(self, proof: Optional[IdentityProof], *, action: str,
                         confirmation_id: str, plan_id: str,
                         plan_hash: str, approved_by: str, nonce: str) -> None:
        if proof is None:
            raise ApprovalIdentityError("远程审批缺少 IdentityProof")
        if proof.nonce != nonce:
            raise ApprovalIdentityError("身份签名 nonce 与审批 nonce 不一致")
        verifier = self.identity_verifier or HMACIdentityVerifier.from_env()
        verified_owner = verifier.verify(
            proof, action=action, confirmation_id=confirmation_id,
            plan_id=plan_id, plan_hash=plan_hash,
        )
        if verified_owner != approved_by:
            raise ApprovalIdentityError(
                f"身份绑定结果 {verified_owner} 与 approved_by={approved_by} 不一致")

    # ── approve（P5：plan_hash 强绑定 + nonce 防重放） ──────────

    def approve(self, confirmation_id: str, approved_by: str,
                plan: ExecutionPlan, expected_plan_hash: str,
                nonce: Optional[str] = None,
                identity_proof: Optional[IdentityProof] = None) -> Confirmation:
        """微信 approve（全部校验通过才产生 APPROVED，任一失败即拒绝）：

        1. confirmation 存在且 status=PENDING（对齐基类）
        2. plan.plan_hash == expected_plan_hash（展示值强绑定，不匹配 → REJECT）
        3. approval_nonce 未被使用（防重放，重复 → REJECT）

        安全实现：nonce 唯一性校验与写库在同一 BEGIN IMMEDIATE 事务内完成，
        关闭 INSERT OR REPLACE 引发的 TOCTOU 窗口（防并发重放）。
        """
        row = dbm.get_confirmation(self.conn, confirmation_id)
        if row is None:
            raise ValueError(f"confirmation 不存在: {confirmation_id}")
        if row["status"] != "PENDING":
            raise ValueError(f"confirmation 状态 {row['status']} 不可批准（只允许 PENDING）")

        # 强绑定：用户在微信上看到的展示值（expected_plan_hash）必须等于系统计划 hash
        if plan.plan_hash != expected_plan_hash:
            raise PlanHashMismatchError(
                f"plan_hash 不匹配（REJECT）：展示值 {expected_plan_hash[:12]}… "
                f"!= 计划值 {plan.plan_hash[:12]}…")

        nonce = nonce or uuid.uuid4().hex
        self._verify_identity(
            identity_proof, action="approve", confirmation_id=confirmation_id,
            plan_id=plan.plan_id, plan_hash=expected_plan_hash,
            approved_by=approved_by, nonce=nonce,
        )

        try:
            row = dbm.approve_confirmation(
                self.conn, confirmation_id, approved_by, self.channel, nonce,
                expected_plan_id=plan.plan_id,
                expected_plan_hash=expected_plan_hash,
            )
        except ValueError as exc:
            if "approval_nonce 已使用" in str(exc):
                raise ReplayError(str(exc)) from exc
            raise
        return Confirmation(**dict(row))

    def _assert_nonce_unused(self, nonce: str) -> None:
        """approval_nonce 唯一性校验（防重放主防线）。

        已用 nonce 集合 = trading_confirmation 表中任意 status 的同 nonce 记录
        （approve 成功后 nonce 会持久化，同 nonce 再次出现即视为重放）。
        DB 层 UNIQUE(approval_nonce) 为兜底约束。
        """
        if dbm.approval_nonce_exists(self.conn, nonce):
            raise ReplayError(f"approval_nonce 已使用（防重放拒绝）: {nonce}")

    # ── reject（显式拒绝动作） ─────────────────────────────────

    def reject(self, confirmation_id: str, approved_by: str = "owner",
               reason: str = "", nonce: Optional[str] = None,
               identity_proof: Optional[IdentityProof] = None) -> Confirmation:
        """微信 reject：仅 PENDING 可拒绝，落库 status=REJECTED + audit。

        nonce 与 approve 一致做唯一性校验（防重放）：复用已用 nonce → ReplayError。
        校验必须发生在任何写库之前——db.insert_confirmation 是 INSERT OR REPLACE，
        若先落库再抛错，会确定性删除同 nonce 的已 APPROVED/CONSUMED 票据。

        安全实现：与 approve 对称，nonce 检查 + 写入在 BEGIN IMMEDIATE 事务内，
        关闭 TOCTOU 竞态窗口。
        """
        row = dbm.get_confirmation(self.conn, confirmation_id)
        if row is None:
            raise ValueError(f"confirmation 不存在: {confirmation_id}")
        if row["status"] != "PENDING":
            raise ValueError(f"confirmation 状态 {row['status']} 不可拒绝（只允许 PENDING）")

        nonce = nonce or uuid.uuid4().hex
        self._verify_identity(
            identity_proof, action="reject", confirmation_id=confirmation_id,
            plan_id=row["plan_id"], plan_hash=row["plan_hash"],
            approved_by=approved_by, nonce=nonce,
        )
        try:
            row = dbm.reject_confirmation(
                self.conn, confirmation_id, approved_by, self.channel, nonce, reason)
        except ValueError as exc:
            if "approval_nonce 已使用" in str(exc):
                raise ReplayError(str(exc)) from exc
            raise
        return Confirmation(**dict(row))
