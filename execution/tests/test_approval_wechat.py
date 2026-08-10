#!/usr/bin/env python3
"""
US-006 Wechat 远程审批 transport — 6 场景单测
==============================================
1. wechat approve 成功 → Confirmation 身份字段完整（D-12）
2. reject 显式拒绝 → status=REJECTED 落库
3. 重放同一 approval_nonce → ReplayError（防重放）
4. plan_hash 与展示值不匹配 → PlanHashMismatchError（强绑定 REJECT）
5. reject 复用已用 nonce → ReplayError 且不删除已 APPROVED 票据
6. reject 复用已 CONSUMED 票据的 nonce → ReplayError 且票据仍在
"""

import sys
import hashlib
import hmac
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from shared import db as dbm
from execution.models import ExecutionPlan, PlanOrder
from execution.order_manager import ConfirmationService, OrderManager
from execution.approval_wechat import (
    WechatApprovalAdapter, ReplayError, PlanHashMismatchError,
    ApprovalIdentityError, HMACIdentityVerifier, IdentityProof,
)


# ────────────────────────────────────────────────────────────────
# helpers / fixtures
# ────────────────────────────────────────────────────────────────

def future(days: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_plan(plan_id="wx_plan", qty=10):
    return ExecutionPlan(
        plan_id=plan_id, account_id="default", execution_mode="DRY_RUN",
        expires_at=future(1),
        orders=[PlanOrder("1", "NVDA.US", "BUY", qty, reference_price=223.96)])


@pytest.fixture
def conn():
    return dbm.get_conn(":memory:")


def create_pending(conn, plan):
    return ConfirmationService(conn).create(plan)


class _TestIdentityVerifier:
    def verify(self, proof, **kwargs):
        if proof.subject != "wx-owner":
            raise ApprovalIdentityError("unknown subject")
        return "owner"


class _TestWechatAdapter(WechatApprovalAdapter):
    _counter = 0

    @classmethod
    def _proof(cls, nonce):
        return IdentityProof("wx-owner", int(time.time()), nonce, "test-signature")

    def approve(self, confirmation_id, approved_by, plan, expected_plan_hash,
                nonce=None, identity_proof=None):
        if nonce is None:
            type(self)._counter += 1
            nonce = f"test-auto-{self._counter}"
        return super().approve(
            confirmation_id, approved_by, plan, expected_plan_hash, nonce,
            identity_proof or self._proof(nonce),
        )

    def reject(self, confirmation_id, approved_by="owner", reason="", nonce=None,
               identity_proof=None):
        if nonce is None:
            type(self)._counter += 1
            nonce = f"test-auto-{self._counter}"
        return super().reject(
            confirmation_id, approved_by, reason, nonce,
            identity_proof or self._proof(nonce),
        )


def _adapter(conn):
    return _TestWechatAdapter(
        conn, channel="wechat", identity_verifier=_TestIdentityVerifier())


# ────────────────────────────────────────────────────────────────
# 场景 1：wechat approve 成功 → 身份字段完整
# ────────────────────────────────────────────────────────────────

def test_wechat_approve_success(conn):
    plan = make_plan()
    cfm = create_pending(conn, plan)
    adapter = _adapter(conn)

    approved = adapter.approve(cfm.confirmation_id, approved_by="owner",
                               plan=plan, expected_plan_hash=plan.plan_hash,
                               nonce="wx_nonce_1")

    # D-12 身份字段完整
    assert approved.status == "APPROVED"
    assert approved.confirmation_id == cfm.confirmation_id
    assert approved.plan_id == plan.plan_id
    assert approved.plan_hash == plan.plan_hash
    assert approved.approved_by == "owner"
    assert approved.approval_channel == "wechat"
    assert approved.approval_nonce == "wx_nonce_1"
    assert approved.approved_at is not None
    assert approved.expires_at is not None
    assert approved.created_at is not None

    # DB 落库一致 + audit lineage
    row = dbm.get_confirmation(conn, cfm.confirmation_id)
    assert row["status"] == "APPROVED"
    assert row["approval_channel"] == "wechat"
    assert row["approval_nonce"] == "wx_nonce_1"
    events = [l["event"] for l in dbm.get_audit(conn, entity_type="confirmation",
                                                entity_id=cfm.confirmation_id)]
    assert "CONFIRMATION_APPROVED" in events

    # 生成的 APPROVED 可正常走 OrderManager 原子消费
    created = OrderManager(conn).consume(plan, approved)
    assert len(created) == 1


# ────────────────────────────────────────────────────────────────
# 场景 2：reject 显式拒绝
# ────────────────────────────────────────────────────────────────

def test_wechat_reject(conn):
    plan = make_plan()
    cfm = create_pending(conn, plan)
    adapter = _adapter(conn)

    rejected = adapter.reject(cfm.confirmation_id, approved_by="owner",
                              reason="user declined")

    assert rejected.status == "REJECTED"
    assert rejected.confirmation_id == cfm.confirmation_id
    assert rejected.plan_id == plan.plan_id
    assert rejected.approval_channel == "wechat"
    assert rejected.approved_at is None  # 未批准无批准时间

    row = dbm.get_confirmation(conn, cfm.confirmation_id)
    assert row["status"] == "REJECTED"

    events = [l["event"] for l in dbm.get_audit(conn, entity_type="confirmation",
                                                entity_id=cfm.confirmation_id)]
    assert "CONFIRMATION_REJECTED" in events

    # REJECTED 不可消费（OrderManager 只接受 APPROVED）
    with pytest.raises(AssertionError):
        OrderManager(conn).consume(plan, rejected)

    # 已拒绝不可再次批准
    with pytest.raises(ValueError):
        adapter.approve(cfm.confirmation_id, approved_by="owner",
                        plan=plan, expected_plan_hash=plan.plan_hash,
                        nonce="wx_nonce_after_reject")


# ────────────────────────────────────────────────────────────────
# 场景 3：重放同一 approval_nonce → 拒绝
# ────────────────────────────────────────────────────────────────

def test_wechat_replay_same_nonce_rejected(conn):
    plan1 = make_plan(plan_id="wx_plan_1")
    cfm1 = create_pending(conn, plan1)
    adapter = _adapter(conn)

    approved = adapter.approve(cfm1.confirmation_id, approved_by="owner",
                               plan=plan1, expected_plan_hash=plan1.plan_hash,
                               nonce="wx_nonce_replay")
    assert approved.status == "APPROVED"

    # 同一 nonce 对另一个 confirmation 重放 → 拒绝
    plan2 = make_plan(plan_id="wx_plan_2")
    cfm2 = create_pending(conn, plan2)
    with pytest.raises(ReplayError):
        adapter.approve(cfm2.confirmation_id, approved_by="owner",
                        plan=plan2, expected_plan_hash=plan2.plan_hash,
                        nonce="wx_nonce_replay")

    # 重放未产生 APPROVED，cfm2 保持 PENDING
    assert dbm.get_confirmation(conn, cfm2.confirmation_id)["status"] == "PENDING"
    assert dbm.list_intents(conn, plan2.plan_id) == []

    # 自动生成 nonce 的两次独立批准互不干扰（非重放路径正常）
    plan3 = make_plan(plan_id="wx_plan_3")
    cfm3 = create_pending(conn, plan3)
    ok = adapter.approve(cfm3.confirmation_id, approved_by="owner",
                         plan=plan3, expected_plan_hash=plan3.plan_hash)
    assert ok.status == "APPROVED" and ok.approval_nonce is not None


# ────────────────────────────────────────────────────────────────
# 场景 4：plan_hash 与展示值不匹配 → 拒绝（强绑定）
# ────────────────────────────────────────────────────────────────

def test_wechat_plan_hash_mismatch_rejected(conn):
    plan = make_plan()
    cfm = create_pending(conn, plan)
    adapter = _adapter(conn)

    # 展示值被篡改 / 外部传入错误 hash → REJECT
    with pytest.raises(PlanHashMismatchError):
        adapter.approve(cfm.confirmation_id, approved_by="owner",
                        plan=plan, expected_plan_hash="deadbeef",
                        nonce="wx_nonce_bad")
    assert dbm.get_confirmation(conn, cfm.confirmation_id)["status"] == "PENDING"

    # 计划已被篡改（数量 10→999），但展示值仍是旧 plan 的 hash → REJECT
    tampered_plan = make_plan(plan_id="wx_plan_tampered", qty=999)
    cfm2 = create_pending(conn, tampered_plan)
    with pytest.raises(PlanHashMismatchError):
        adapter.approve(cfm2.confirmation_id, approved_by="owner",
                        plan=tampered_plan, expected_plan_hash=plan.plan_hash,
                        nonce="wx_nonce_bad2")
    assert dbm.get_confirmation(conn, cfm2.confirmation_id)["status"] == "PENDING"
    assert dbm.list_intents(conn, tampered_plan.plan_id) == []

    # 修正 expected_plan_hash 后仍可正常批准（强绑定通过）
    ok = adapter.approve(cfm2.confirmation_id, approved_by="owner",
                         plan=tampered_plan, expected_plan_hash=tampered_plan.plan_hash,
                         nonce="wx_nonce_ok")
    assert ok.status == "APPROVED"


# ────────────────────────────────────────────────────────────────
# 场景 5：reject 复用已用 nonce → ReplayError 且不删除已 APPROVED 票据
# ────────────────────────────────────────────────────────────────

def test_wechat_reject_replay_nonce_preserves_approved(conn):
    plan1 = make_plan(plan_id="wx_plan_1")
    cfm1 = create_pending(conn, plan1)
    adapter = _adapter(conn)

    approved = adapter.approve(cfm1.confirmation_id, approved_by="owner",
                               plan=plan1, expected_plan_hash=plan1.plan_hash,
                               nonce="wx_nonce_shared")
    assert approved.status == "APPROVED"

    # 对抗场景：调用方给 reject 传入已用 nonce
    # 旧缺陷：insert_confirmation 的 INSERT OR REPLACE 会确定性删除 cfm1 的
    # APPROVED 主表记录（审计链仅剩 audit_log 事件）
    plan2 = make_plan(plan_id="wx_plan_2")
    cfm2 = create_pending(conn, plan2)
    with pytest.raises(ReplayError):
        adapter.reject(cfm2.confirmation_id, approved_by="owner",
                       reason="replayed reject", nonce="wx_nonce_shared")

    # 已 APPROVED 票据仍在且身份字段未被破坏
    row1 = dbm.get_confirmation(conn, cfm1.confirmation_id)
    assert row1 is not None
    assert row1["status"] == "APPROVED"
    assert row1["approval_nonce"] == "wx_nonce_shared"
    assert row1["approved_at"] is not None

    # 被重放拒绝的 cfm2 保持 PENDING（校验在写库前，未落 REJECTED）
    row2 = dbm.get_confirmation(conn, cfm2.confirmation_id)
    assert row2 is not None
    assert row2["status"] == "PENDING"

    # 新 nonce 的 reject 仍正常（正常拒绝路径不受影响）
    rejected = adapter.reject(cfm2.confirmation_id, approved_by="owner",
                              reason="user declined", nonce="wx_nonce_fresh")
    assert rejected.status == "REJECTED"
    assert dbm.get_confirmation(conn, cfm2.confirmation_id)["status"] == "REJECTED"


# ────────────────────────────────────────────────────────────────
# 场景 6：reject 复用已 CONSUMED 票据的 nonce → ReplayError 且票据仍在
# ────────────────────────────────────────────────────────────────

def test_wechat_reject_replay_nonce_preserves_consumed(conn):
    plan1 = make_plan(plan_id="wx_plan_1")
    cfm1 = create_pending(conn, plan1)
    adapter = _adapter(conn)

    approved = adapter.approve(cfm1.confirmation_id, approved_by="owner",
                               plan=plan1, expected_plan_hash=plan1.plan_hash,
                               nonce="wx_nonce_consumed")
    OrderManager(conn).consume(plan1, approved)  # 消费后 status=CONSUMED
    assert dbm.get_confirmation(conn, cfm1.confirmation_id)["status"] == "CONSUMED"

    plan2 = make_plan(plan_id="wx_plan_2")
    cfm2 = create_pending(conn, plan2)
    with pytest.raises(ReplayError):
        adapter.reject(cfm2.confirmation_id, approved_by="owner",
                       reason="replayed reject", nonce="wx_nonce_consumed")

    # 已 CONSUMED 票据仍在（未被 REPLACE 删除）
    row1 = dbm.get_confirmation(conn, cfm1.confirmation_id)
    assert row1 is not None
    assert row1["status"] == "CONSUMED"
    assert row1["approval_nonce"] == "wx_nonce_consumed"

    # cfm2 保持 PENDING
    assert dbm.get_confirmation(conn, cfm2.confirmation_id)["status"] == "PENDING"


def test_hmac_identity_verifier_binds_subject_plan_nonce_and_time():
    secret = "s" * 32
    verifier = HMACIdentityVerifier(secret, {"wx-openid-1": "owner"})
    proof = IdentityProof("wx-openid-1", int(time.time()), "nonce-hmac", "")
    payload = verifier.payload(
        proof, action="approve", confirmation_id="cfm1",
        plan_id="p1", plan_hash="hash1",
    )
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    signed = IdentityProof(proof.subject, proof.timestamp, proof.nonce, signature)
    assert verifier.verify(
        signed, action="approve", confirmation_id="cfm1",
        plan_id="p1", plan_hash="hash1",
    ) == "owner"
    with pytest.raises(ApprovalIdentityError):
        verifier.verify(
            signed, action="approve", confirmation_id="cfm1",
            plan_id="p1", plan_hash="tampered",
        )
