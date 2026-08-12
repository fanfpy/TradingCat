#!/usr/bin/env python3
"""
Execution 层核心模型 — 交易系统 v4.0（架构 D-3 / D-8 / D-9 / D-12）
=================================================================
数据契约（不可变 + 可审计）：

ID 链（D-9）：
    ExecutionPlan(plan_id) → PlanOrder(plan_order_id) → Confirmation(confirmation_id)
    → OrderIntent(client_request_id UNIQUE) → Broker(broker_order_id) → Fill

核心不变量：
- ExecutionPlan 不可变；plan_hash = sha256(account_id + execution_mode + orders[] + expires_at)
- 任何字段变化 → plan_hash 变 → 原 Confirmation 失效（INVALID）
- execution_mode 进 plan_hash：DRY_RUN → LIVE 必须新 plan_id + 新人工确认
- Confirmation 单次消费（状态机 PENDING→APPROVED→CONSUMED|EXPIRED|REJECTED|CANCELLED）
"""

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

from datetime import datetime, timezone


EXECUTION_MODES = ("DRY_RUN", "PAPER", "LIVE")
APPROVAL_PROOF_CHANNEL = "approval-proof"

# ────────────────────────────────────────────────────────────────
# 时间工具
# ────────────────────────────────────────────────────────────────

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ────────────────────────────────────────────────────────────────
# PlanOrder / ExecutionPlan（D-3）
# ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PlanOrder:
    plan_order_id: str          # 例 "1"
    symbol: str
    side: str                   # BUY|SELL
    quantity: float
    order_type: str = "MARKET"  # MARKET|LIMIT
    reference_price: Optional[float] = None
    reference_quote_at: Optional[str] = None
    max_slippage_bps: float = 50.0
    strategy_version_id: Optional[int] = None
    investor_policy_version_id: Optional[int] = None

    def to_dict(self) -> Dict:
        payload = asdict(self)
        # v5 新字段仅在实际绑定策略时进入 canonical hash；None 时保持 v4 已持久化
        # 计划的 hash 兼容性。
        if self.investor_policy_version_id is None:
            payload.pop("investor_policy_version_id")
        return payload


@dataclass(frozen=True)
class ExecutionPlan:
    plan_id: str
    account_id: str
    execution_mode: str          # DRY_RUN|PAPER|LIVE
    expires_at: str
    orders: tuple[PlanOrder, ...]
    plan_hash: str = ""

    def __post_init__(self) -> None:
        assert self.execution_mode in EXECUTION_MODES, f"非法 execution_mode: {self.execution_mode}"
        object.__setattr__(self, "orders", tuple(self.orders))
        # frozen dataclass 下用 object.__setattr__ 计算不可变 hash
        object.__setattr__(self, "plan_hash", compute_plan_hash(
            self.account_id, self.execution_mode,
            [o.to_dict() for o in sorted(self.orders, key=lambda x: x.plan_order_id)],
            self.expires_at))

    def is_expired(self, at: Optional[str] = None) -> bool:
        # 到期瞬间即失效；不能让同一秒的计划在边界条件下继续提交。
        return parse_ts(self.expires_at) <= parse_ts(at or now_utc())

    def to_dict(self) -> Dict:
        return {
            "plan_id": self.plan_id,
            "account_id": self.account_id,
            "execution_mode": self.execution_mode,
            "expires_at": self.expires_at,
            "orders": [o.to_dict() for o in sorted(self.orders, key=lambda x: x.plan_order_id)],
            "plan_hash": self.plan_hash,
        }


def compute_plan_hash(account_id: str, execution_mode: str, orders: List[Dict], expires_at: str) -> str:
    """plan_hash 覆盖 account_id + execution_mode + orders[] + expires_at（全部不可变）。"""
    canonical = json.dumps(
        {"account_id": account_id, "execution_mode": execution_mode,
         "orders": orders, "expires_at": expires_at},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


# ────────────────────────────────────────────────────────────────
# Confirmation（D-3 / D-12）
# ────────────────────────────────────────────────────────────────

CONFIRMATION_STATUSES = ("PENDING", "APPROVED", "CONSUMED", "EXPIRED", "REJECTED", "CANCELLED")


@dataclass(frozen=True)
class Confirmation:
    confirmation_id: str
    plan_id: str
    plan_hash: str
    status: str = "PENDING"
    approved_by: Optional[str] = None        # 例 owner
    approval_channel: Optional[str] = None   # 例 cli|wechat
    approval_nonce: Optional[str] = None     # 防 replay
    approved_at: Optional[str] = None
    expires_at: Optional[str] = None
    created_at: Optional[str] = None

    def is_expired(self, at: Optional[str] = None) -> bool:
        """到期瞬间即失效，与 ExecutionPlan 使用同一边界规则。"""
        return bool(self.expires_at and
                    parse_ts(self.expires_at) <= parse_ts(at or now_utc()))

    def to_dict(self) -> Dict:
        return asdict(self)


# ────────────────────────────────────────────────────────────────
# MarketState（D-8 quote 新鲜度）
# ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MarketState:
    symbol: str
    quote_at: str
    price: float
    max_age_seconds: int = 300

    def is_fresh(self, at: Optional[str] = None) -> bool:
        age = (parse_ts(at or now_utc()) - parse_ts(self.quote_at)).total_seconds()
        return 0 <= age <= self.max_age_seconds


# ────────────────────────────────────────────────────────────────
# PreTradeRiskResult（D-8：只允许 PASS | REJECT）
# ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PreTradeRiskResult:
    decision: str               # PASS | REJECT
    reasons: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.decision == "PASS"
