#!/usr/bin/env python3
"""
US-004 — tc trade 交互式 CLI 接入 Execution 链（DRY_RUN）
========================================================
覆盖（复用 execution 链，不依赖真实 stdin / 真实 DB）：
1. 确认路径（y）→ 成功生成 OrderIntent（confirmation CONSUMED）
2. 拒绝路径（N）→ 零 OrderIntent（confirmation CANCELLED）
3. 无效 confirmation（plan_hash 不匹配）→ PTR REJECT，零 OrderIntent
4. --mode LIVE → 二次确认后仍默认拒绝，零 OrderIntent
5. 行情缺失路径 → 不抛裸 NameError（US-004），PTR 拒绝、零 OrderIntent、友好错误
6. qty=0 / 负数 → 拒绝（argparse 层 + _run_trade_order 兜底），零 OrderIntent
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from shared import db as dbm
from execution.models import Confirmation
from execution.order_manager import ApprovalAdapter
from execution.broker import BrokerEventHandler
from execution.broker_live import LiveBroker
from tc import _build_plan, _fetch_quote, _post_approval, _positive_float, _run_trade_order


@pytest.fixture
def conn():
    """内存 DB + SYNCED 账户（PTR 通过的前提）。"""
    c = dbm.get_conn(":memory:")
    dbm.upsert_account(c, "default", "SYNCED", cash=100_000.0, buying_power=50_000.0)
    return c


def provider(price=224.0):
    """注入的行情提供器：不触网，返回固定参考价 + 新鲜 quote_at。"""
    from execution.models import now_utc
    return lambda conn, symbol: (price, now_utc())


# ────────────────────────────────────────────────────────────────
# 1. 确认路径（y）→ 生成 OrderIntent
# ────────────────────────────────────────────────────────────────

def test_confirm_path_y_creates_intent(conn):
    rc = _run_trade_order(conn, "NVDA.US", 10, "DRY_RUN",
                          confirm_input=lambda: True, quote_provider=provider())
    assert rc == 0
    intents = dbm.list_intents(conn)
    assert len(intents) == 1
    it = intents[0]
    assert it["symbol"] == "NVDA.US"
    assert it["side"] == "BUY"
    assert it["quantity"] == 10
    assert it["status"] == "PENDING"
    assert it["reference_price"] == 224.0
    assert it["max_slippage_bps"] == 50.0
    # confirmation 一次性消费 → CONSUMED
    rows = conn.execute("SELECT * FROM trading_confirmation").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "CONSUMED"
    assert rows[0]["approved_by"] == "owner"
    assert rows[0]["approval_channel"] == "cli"
    assert rows[0]["approval_nonce"]  # nonce 随机生成


# ────────────────────────────────────────────────────────────────
# 2. 拒绝路径（N）→ 零 OrderIntent
# ────────────────────────────────────────────────────────────────

def test_reject_path_n_zero_intent(conn):
    rc = _run_trade_order(conn, "NVDA.US", 10, "DRY_RUN",
                          confirm_input=lambda: False, quote_provider=provider())
    assert rc == 0
    assert dbm.list_intents(conn) == []
    rows = conn.execute("SELECT * FROM trading_confirmation").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "CANCELLED"


# ────────────────────────────────────────────────────────────────
# 3. 无效 confirmation（plan_hash 不匹配）→ 拒绝，零 OrderIntent
# ────────────────────────────────────────────────────────────────

def test_invalid_confirmation_plan_hash_mismatch_rejected(conn):
    plan, cfm = _build_plan(conn, "NVDA.US", 10, "DRY_RUN", provider())
    approved = ApprovalAdapter(conn, channel="cli").approve(
        cfm.confirmation_id, approved_by="owner", nonce="n_badhash")
    # 篡改 plan_hash → 无效 confirmation（D-3：任何字段变化 → 失效）
    tampered = Confirmation(confirmation_id=approved.confirmation_id,
                            plan_id=approved.plan_id,
                            plan_hash="deadbeef", status="APPROVED")
    rc, created = _post_approval(conn, plan, tampered, provider())
    assert rc != 0
    assert created == []
    assert dbm.list_intents(conn) == [], "无效 confirmation 不得产生任何 OrderIntent"
    # 审计留痕：confirmation 置 REJECTED
    rows = conn.execute("SELECT * FROM trading_confirmation").fetchall()
    assert rows[0]["status"] == "REJECTED"


# ────────────────────────────────────────────────────────────────
# 4. --mode LIVE → 二次确认后仍默认拒绝（铁律：无 broker 客户端不提交）
# ────────────────────────────────────────────────────────────────

def test_live_mode_default_rejected_even_with_confirm(conn):
    rc = _run_trade_order(conn, "NVDA.US", 10, "LIVE",
                          confirm_input=lambda: True, quote_provider=provider())
    assert rc != 0
    assert dbm.list_intents(conn) == [], "LIVE 无券商客户端默认拒绝，零 OrderIntent"
    rows = conn.execute("SELECT * FROM trading_confirmation").fetchall()
    assert rows == [], "单库模式不得创建 executiond-owned Confirmation"


def test_live_cli_exact_phrase_still_requires_executiond_approval_proof(conn):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def order(self, side, symbol, qty, **kwargs):
            self.calls.append((side, symbol, qty, kwargs))
            return {"success": True, "order_id": "mock-live-1",
                    "status": "Submitted"}

    execution_conn = dbm.get_conn(":memory:")
    dbm.mark_system_readiness(execution_conn, "P0_A", "pytest-p0-a")
    from datetime import datetime, timezone, timedelta
    canary_expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    dbm.create_live_canary(execution_conn, "default", ["NVDA.US"], "BUY",
                           10_000, 1, canary_expiry)
    fake = FakeClient()
    broker = LiveBroker(execution_conn, client=fake, enable_live=True,
                        event_handler=BrokerEventHandler(execution_conn))
    rc = _run_trade_order(
        conn, "NVDA.US", 10, "LIVE", confirm_input=lambda: True,
        quote_provider=provider(), enable_live=True,
        live_confirm_input=lambda plan_id: f"LIVE {plan_id}", broker=broker,
        execution_conn=execution_conn,
    )
    assert rc != 0
    assert len(fake.calls) == 0
    assert dbm.list_intents(conn) == []
    assert dbm.list_intents(execution_conn) == []
    assert execution_conn.execute(
        "SELECT status FROM trading_confirmation").fetchone()[0] == "PENDING"


def test_live_mode_wrong_phrase_is_zero_intent(conn):
    execution_conn = dbm.get_conn(":memory:")
    rc = _run_trade_order(
        conn, "NVDA.US", 10, "LIVE", confirm_input=lambda: True,
        quote_provider=provider(), enable_live=True,
        live_confirm_input=lambda plan_id: "LIVE wrong-plan",
        execution_conn=execution_conn,
    )
    assert rc != 0
    assert dbm.list_intents(execution_conn) == []


# ────────────────────────────────────────────────────────────────
# 5. 行情缺失路径（US-004 修复：_fetch_quote 缺 dbm import → 裸 NameError）
# ────────────────────────────────────────────────────────────────

def _no_quote_provider():
    """注入的行情提供器：行情完全缺失（返回 (None, None)），模拟错别字/退市/CLI 失败。"""
    return lambda conn, symbol: (None, None)


def test_fetch_quote_missing_quote_no_nameerror(monkeypatch, conn):
    """回归：longbridge SDK 失败 + DB 无快照/无日线 → 返回 (None, None)，绝不抛 NameError。

    US-004 修复前：_fetch_quote 未 import dbm，fallback 路径抛裸 NameError。
    """
    class BrokenClient:
        def __init__(self):
            raise RuntimeError("SDK credentials unavailable")

    monkeypatch.setattr("shared.longbridge_client.LongbridgeClient", BrokenClient)
    price, quote_at = _fetch_quote(conn, "ZZZZZ.US")
    assert price is None
    assert quote_at is None


def test_missing_quote_confirm_y_friendly_reject_zero_intent(conn, capsys):
    """端到端：行情缺失 + 用户确认 y → 友好拒绝（PTR 无 MarketState），零 OrderIntent，非 0 退出。"""
    rc = _run_trade_order(conn, "ZZZZZ.US", 1, "DRY_RUN",
                          confirm_input=lambda: True, quote_provider=_no_quote_provider())
    assert rc != 0
    assert dbm.list_intents(conn) == [], "行情缺失不得产生任何 OrderIntent"
    out = capsys.readouterr().out
    assert "拒绝" in out and "无 MarketState" in out, "应友好报错（PTR 拒绝原因），非裸 traceback"


# ────────────────────────────────────────────────────────────────
# 6. qty 校验（US-004 修复：--qty 0 / 负数必须被拒绝，零 OrderIntent）
# ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_qty", [0, -5.0])
def test_qty_non_positive_rejected_zero_intent(conn, bad_qty):
    rc = _run_trade_order(conn, "TEST.US", bad_qty, "DRY_RUN",
                          confirm_input=lambda: True, quote_provider=provider())
    assert rc != 0, f"qty={bad_qty} 必须被拒绝"
    assert dbm.list_intents(conn) == [], f"qty={bad_qty} 不得产生任何 OrderIntent"


@pytest.mark.parametrize("bad", ["0", "-5", "abc"])
def test_argparse_positive_float_rejects_invalid(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_float(bad)


def test_argparse_positive_float_accepts_valid():
    assert _positive_float("10") == 10.0
    assert _positive_float("0.5") == 0.5
