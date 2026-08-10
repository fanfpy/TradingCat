#!/usr/bin/env python3
"""Decision 编排：Signal → PositionSizer → TargetPortfolio → ExecutionPlan。"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from execution.models import ExecutionPlan, PlanOrder, now_utc
from production.monitor import post_market_check
from production.portfolio_risk import PositionPlan
from production.position import KellyPositionSizer, PositionIntent
from production.target_portfolio import TargetPortfolio, build_target_portfolio
from shared import db as dbm
from shared.indicators import atr22
from shared.cost_model import estimate_cost


ELIGIBLE_LIFECYCLE = ("verified", "live")


def _params(row) -> Optional[Dict]:
    if row is None or not row["params_json"]:
        return None
    try:
        params = json.loads(row["params_json"])
    except (TypeError, ValueError):
        return None
    required = ("entry_mode", "ma_period", "atr_multiple", "buffer")
    return params if all(key in params for key in required) else None


def _positions(account_state, conn) -> List[Dict]:
    if account_state is not None and getattr(account_state, "positions", None):
        return list(account_state.positions)
    return [dict(row) for row in dbm.list_positions(conn)]


def collect_signals(conn, account_state=None, as_of_date: Optional[str] = None,
                    account_id: str = "default") -> List[Dict]:
    """收集受 lifecycle gate 控制的入场和退出信号，不伪造研究证据。"""
    date = as_of_date or datetime.now().strftime("%Y-%m-%d")
    lifecycle = {row["symbol"]: row for row in dbm.list_lifecycle(conn)}
    positions = _positions(account_state, conn)
    position_map = {p.get("symbol", ""): p for p in positions}
    symbols = {s for s, row in lifecycle.items() if row["status"] in ELIGIBLE_LIFECYCLE}
    symbols.update(position_map)
    signals: List[Dict] = []

    for symbol in sorted(symbols):
        row = lifecycle.get(symbol)
        # suspended/removed/degraded 永不产生新入场；已有持仓仍允许退出。
        eligible_entry = row is not None and row["status"] in ELIGIBLE_LIFECYCLE
        params = _params(row)
        if params is None:
            dbm.audit(conn, "SIGNAL_SKIPPED", "symbol", symbol,
                      {"reason": "missing_strategy_params"})
            continue
        bars = dbm.get_bars(conn, symbol)
        if len(bars) < 50:
            continue
        realtime_position = position_map.get(symbol)
        report = post_market_check(conn, symbol, params, date,
                                   realtime_position=realtime_position)
        latest = dbm.get_latest_strategy_version(conn, symbol)
        strategy_version_id = latest["version_id"] if latest is not None else None

        if report.exit_triggered and realtime_position is not None:
            signal = {
                "symbol": symbol, "kind": "EXIT", "target_fraction": 0.0,
                "entry_price": float(bars[-1]["close"]), "stop_price": None,
                "strategy_version_id": strategy_version_id,
                "rationale": "; ".join(report.messages),
            }
            persisted = dbm.record_signal_with_outbox(
                conn, account_id=account_id, symbol=symbol,
                strategy_version_id=int(strategy_version_id or 0),
                bar_ts=bars[-1]["ts"], signal_type="EXIT", payload=signal,
            )
            signal["signal_event_id"] = persisted["event"]["event_id"]
            signals.append(signal)
            continue

        if not (eligible_entry and report.formal_entry):
            continue
        manifest = dbm.get_manifest(conn, symbol)
        if (manifest is None
                or manifest["adjustment_mode"] not in ("FORWARD", "TEST")
                or manifest["corporate_actions_status"] not in (
                    "SYNCED", "PROVIDER_ADJUSTED", "TEST")):
            dbm.audit(conn, "SIGNAL_SKIPPED", "symbol", symbol,
                      {"reason": "corporate_actions_or_adjustment_unknown"})
            continue
        if latest is None or not latest["oos_stats_json"]:
            dbm.audit(conn, "SIGNAL_SKIPPED", "symbol", symbol,
                      {"reason": "missing_oos_stats"})
            continue
        try:
            oos_stats = json.loads(latest["oos_stats_json"])
        except (TypeError, ValueError):
            continue
        if oos_stats.get("final_test_accepted") is not True:
            dbm.audit(conn, "SIGNAL_SKIPPED", "symbol", symbol,
                      {"reason": "strategy_final_test_not_accepted",
                       "strategy_version_id": strategy_version_id})
            continue
        entry = float(bars[-1]["close"])
        atr = atr22([b["high"] for b in bars], [b["low"] for b in bars],
                    [b["close"] for b in bars])[-1]
        signal = {
            "symbol": symbol, "kind": "ENTRY", "oos_stats": oos_stats,
            "entry_price": entry,
            "stop_price": entry - float(params["atr_multiple"]) * atr,
            "strategy_version_id": strategy_version_id,
            "rationale": "; ".join(report.messages),
        }
        persisted = dbm.record_signal_with_outbox(
            conn, account_id=account_id, symbol=symbol,
            strategy_version_id=int(strategy_version_id or 0),
            bar_ts=bars[-1]["ts"], signal_type="ENTRY", payload=signal,
        )
        signal["signal_event_id"] = persisted["event"]["event_id"]
        signals.append(signal)
    return signals


def _existing_plans(conn, equity: float, account_state=None) -> List[PositionPlan]:
    plans: List[PositionPlan] = []
    for position in _positions(account_state, conn):
        symbol = position.get("symbol", "")
        qty = float(position.get("quantity", 0) or 0)
        bars = dbm.get_bars(conn, symbol)
        price = float(position.get("last_price", 0) or 0)
        if price <= 0 and bars:
            price = float(bars[-1]["close"])
        if not symbol or qty <= 0 or price <= 0 or equity <= 0:
            continue
        security = dbm.get_security(conn, symbol)
        dollar = sorted(float(row["close"]) * float(row["volume"])
                        for row in bars[-60:] if row["close"] and row["volume"])
        plans.append(PositionPlan(
            symbol=symbol, target_frac=qty * price / equity,
            stop_price=float(position.get("stop_price", 0) or 0) or None,
            entry_price=float(position.get("cost_price", 0) or
                              position.get("entry_price", 0) or price),
            is_proposed=False,
            sector=security["sector"] if security is not None else "UNKNOWN",
            currency=security["currency"] if security is not None else "UNKNOWN",
            asset_type=security["asset_type"] if security is not None else "EQUITY",
            beta=float(security["beta"]) if security is not None else 1.0,
            leverage=float(security["leverage"]) if security is not None else 1.0,
            median_dollar_volume=dollar[len(dollar) // 2] if dollar else None,
        ))
    return plans


def run_decision(conn, equity: float, account_state=None,
                 as_of_date: Optional[str] = None) -> TargetPortfolio:
    """运行完整决策链，输出目标组合；AccountState 已提供时必须为 SYNCED。"""
    account_id = getattr(account_state, "account_id", "default") if account_state else "default"
    policy_row = dbm.get_active_investor_policy(conn, account_id)
    policy = json.loads(policy_row["config_json"])
    signals = collect_signals(conn, account_state=account_state, as_of_date=as_of_date)
    intents: List[PositionIntent] = []
    sizer = KellyPositionSizer(policy)
    for signal in signals:
        if signal["kind"] == "EXIT":
            intents.append(PositionIntent(
                symbol=signal["symbol"], side="SELL", target_fraction=0.0,
                entry_price=signal["entry_price"], stop_price=None,
                rationale=signal["rationale"],
                evidence={"strategy_version_id": signal["strategy_version_id"],
                          "signal_kind": "EXIT"},
            ))
        else:
            sized = sizer.size(signal, equity, account_state)
            for intent in sized:
                intent.evidence["strategy_version_id"] = signal["strategy_version_id"]
                intent.evidence["signal_kind"] = "ENTRY"
                intents.append(intent)
        dbm.audit(conn, "POSITION_INTENT", "symbol", signal["symbol"],
                  {"intents": [intent.__dict__ for intent in intents
                               if intent.symbol == signal["symbol"]]})

    tp = build_target_portfolio(
        conn, equity, intents,
        existing_positions=_existing_plans(conn, equity, account_state),
        account_state=account_state,
        policy=policy,
    )
    tp.details["signal_count"] = len(signals)
    tp.details["investor_policy_version_id"] = policy_row["policy_version_id"]
    tp.details["investor_policy_hash"] = policy_row["config_hash"]
    return tp


def target_to_execution_plan(conn, tp: TargetPortfolio, equity: float,
                             account_id: str = "default", mode: str = "DRY_RUN",
                             account_state=None) -> Optional[ExecutionPlan]:
    """把目标权重转换成相对真实持仓和挂单的差额订单，并持久化不可变 Plan。"""
    if mode not in ("DRY_RUN", "LIVE"):
        raise ValueError(f"非法 execution mode: {mode}")
    current = {p.get("symbol", ""): float(p.get("quantity", 0) or 0)
               for p in _positions(account_state, conn)}
    policy_row = dbm.get_active_investor_policy(conn, account_id)
    pending: Dict[str, float] = {}
    for row in dbm.list_intents(conn):
        if row["status"] not in ("PENDING", "SUBMITTING", "SUBMITTED", "UNKNOWN"):
            continue
        signed = float(row["quantity"]) if row["side"] == "BUY" else -float(row["quantity"])
        pending[row["symbol"]] = pending.get(row["symbol"], 0.0) + signed
    intents = {intent.symbol: intent for intent in tp.intents}
    orders: List[PlanOrder] = []
    for symbol, fraction in sorted(tp.final_fracs.items()):
        intent = intents.get(symbol)
        bars = dbm.get_bars(conn, symbol)
        price = float(intent.entry_price or 0) if intent is not None else 0.0
        if price <= 0 and bars:
            price = float(bars[-1]["close"])
        if price <= 0:
            continue
        target_qty = int(max(0.0, fraction) * equity // price)
        delta = target_qty - current.get(symbol, 0.0) - pending.get(symbol, 0.0)
        quantity = int(abs(delta))
        if quantity <= 0:
            continue
        strategy_version_id = None
        if intent is not None:
            strategy_version_id = intent.evidence.get("strategy_version_id")
        expected_notional = quantity * price
        cost = estimate_cost(
            symbol, [float(row["close"]) for row in bars],
            [float(row["volume"]) for row in bars], expected_notional)
        orders.append(PlanOrder(
            plan_order_id=f"po_{len(orders) + 1:03d}", symbol=symbol,
            side="BUY" if delta > 0 else "SELL", quantity=quantity,
            order_type="LIMIT", reference_price=price,
            reference_quote_at=bars[-1]["ts"] if bars else now_utc(),
            max_slippage_bps=min(100.0, max(20.0, cost.total_bps_per_side * 2.0)),
            strategy_version_id=strategy_version_id,
            investor_policy_version_id=policy_row["policy_version_id"],
        ))
    if not orders:
        return None
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    plan = ExecutionPlan(
        plan_id=f"plan_{uuid.uuid4().hex[:12]}", account_id=account_id,
        execution_mode=mode, expires_at=expires_at, orders=tuple(orders),
    )
    dbm.insert_plan(conn, plan.plan_id, account_id, mode, expires_at, plan.plan_hash,
                    [order.to_dict() for order in orders])
    from production.notification import safe_notify
    safe_notify(
        conn, "execution_plan.created", f"ExecutionPlan {plan.plan_id} 待审批",
        f"mode={mode}, orders={len(orders)}, plan_hash={plan.plan_hash}",
        severity="WARNING" if mode == "LIVE" else "INFO",
        entity_type="plan", entity_id=plan.plan_id,
    )
    return plan


def load_execution_plan(conn, plan_id: str) -> Optional[ExecutionPlan]:
    """从 StateRepository 恢复此前生成并展示给用户的同一个 Plan。"""
    row = dbm.get_plan(conn, plan_id)
    if row is None:
        return None
    orders = tuple(PlanOrder(**item) for item in json.loads(row["orders_json"]))
    plan = ExecutionPlan(row["plan_id"], row["account_id"], row["execution_mode"],
                         row["expires_at"], orders)
    if plan.plan_hash != row["plan_hash"]:
        raise RuntimeError("持久化 ExecutionPlan hash 校验失败")
    return plan
