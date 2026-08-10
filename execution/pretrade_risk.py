#!/usr/bin/env python3
"""
PreTradeRisk — 交易系统 v4.0（架构 D-8）
======================================
确认后提交前的最后一道闸门。只允许 PASS 或 REJECT，**禁止修改任何已批准订单字段**
（"缩减"本身就是修改计划，同样违反人工审批不可绕过铁律——人批准 BUY 20，风控只能买 12
→ REJECT + 生成建议"当前最多 BUY 12" → 新 ExecutionPlan → 新 Human Confirmation）。

输入四件套（缺一不可）：
    ExecutionPlan + Confirmation + AccountState + MarketState(s)

校验项（全部通过才 PASS）：
1. confirmation.status == APPROVED 且未消费
2. confirmation.plan_hash == plan.plan_hash（任何字段变化 → 失效）
3. plan 未过期（expires_at > now）
4. AccountState.sync_status == SYNCED（D-7：非 SYNCED 必须 REJECT 已批准订单）
5. quote 新鲜（now - quote_at <= max_age；每只订单 symbol 都要）
6. slippage 可接受（参考价已含在 plan；此处校验参考价未偏离市场价超过 max_slippage_bps）
7. 现金/购买力足够（BUY 合计名义 <= buying_power）
8. 挂单敞口可接受（pending intent 数不超上限）
"""

from typing import Dict, List, Optional

from execution.models import ExecutionPlan, Confirmation, MarketState, PreTradeRiskResult, now_utc, parse_ts

MAX_PENDING_INTENTS = 10  # 同一账户最多挂单数（防失控堆积）


def evaluate(plan: ExecutionPlan, confirmation: Confirmation,
             account_state, market_states: Dict[str, MarketState],
             pending_intents: int = 0, unknown_intents: int = 0) -> PreTradeRiskResult:
    """执行 PreTradeRisk 校验。只返回 PASS/REJECT + 原因，绝不修改任何字段。

    unknown_intents: 处于 UNKNOWN 状态的现有订单数（D-9：订单状态 UNKNOWN 时禁止新订单）。
    """
    reasons: List[str] = []

    # 1. Confirmation 状态
    if confirmation.status != "APPROVED":
        reasons.append(f"confirmation 状态 {confirmation.status} != APPROVED")
    # 2. plan_hash 强绑定（D-3：任何字段变化 → INVALID）
    if confirmation.plan_hash != plan.plan_hash:
        reasons.append("confirmation.plan_hash 与 plan.plan_hash 不匹配（计划已被修改）")
    if confirmation.plan_id != plan.plan_id:
        reasons.append("confirmation.plan_id 与 plan.plan_id 不匹配")
    # 3. 过期
    if plan.is_expired():
        reasons.append(f"plan 已过期（expires_at={plan.expires_at}）")
    if confirmation.expires_at and parse_ts(confirmation.expires_at) < parse_ts(now_utc()):
        reasons.append(f"confirmation 已过期（expires_at={confirmation.expires_at}）")
    # 4. AccountState（D-7 硬条件）
    if account_state is None:
        reasons.append("账户状态未知（AccountState 缺失）")
    elif account_state.sync_status != "SYNCED":
        reasons.append(f"AccountState.sync_status={account_state.sync_status} != SYNCED（账户状态不确定则禁止执行）")
    # 5. quote 新鲜（每只订单 symbol）
    for o in plan.orders:
        if o.side not in ("BUY", "SELL"):
            reasons.append(f"{o.symbol} side={o.side} 非法")
        if o.quantity <= 0:
            reasons.append(f"{o.symbol} quantity 必须 > 0")
        if o.order_type not in ("MARKET", "LIMIT"):
            reasons.append(f"{o.symbol} order_type={o.order_type} 非法")
        if o.max_slippage_bps < 0:
            reasons.append(f"{o.symbol} max_slippage_bps 不能为负")
        ms = market_states.get(o.symbol)
        if ms is None:
            reasons.append(f"{o.symbol} 无 MarketState（缺 quote）")
        elif not ms.is_fresh():
            reasons.append(f"{o.symbol} quote 过期（quote_at={ms.quote_at}）")
    # 6. slippage 可接受：reference_price 偏离当前市场价不超过 max_slippage_bps
    for o in plan.orders:
        if o.reference_price is None:
            continue
        ms = market_states.get(o.symbol)
        if ms is None or ms.price <= 0:
            continue
        deviation = abs(ms.price - o.reference_price) / ms.price * 10_000
        if deviation > o.max_slippage_bps:
            reasons.append(
                f"{o.symbol} 参考价 {o.reference_price} 偏离市场价 {ms.price} "
                f"{deviation:.0f}bps > {o.max_slippage_bps}bps")
    # 7. 现金/购买力足够（BUY 合计名义）
    has_buy = any(o.side == "BUY" for o in plan.orders)
    if account_state is not None and has_buy and account_state.buying_power is None:
        reasons.append("购买力未知（BUY 订单禁止执行）")
    elif account_state is not None and account_state.buying_power is not None:
        total_notional = sum(o.quantity * ms.price
                             for o in plan.orders if o.side == "BUY"
                             and (ms := market_states.get(o.symbol)) is not None and ms.price > 0)
        if total_notional > account_state.buying_power:
            reasons.append(f"购买力不足：需 {total_notional:.2f} > 可用 {account_state.buying_power:.2f}")
    # 8. 挂单敞口
    if pending_intents >= MAX_PENDING_INTENTS:
        reasons.append(f"挂单敞口已达上限 {MAX_PENDING_INTENTS}")
    # 9. 订单状态 UNKNOWN → 禁止新订单（D-9 fail closed）
    if unknown_intents > 0:
        reasons.append(f"存在 {unknown_intents} 个 UNKNOWN 状态订单，禁止提交新订单")

    if reasons:
        return PreTradeRiskResult(decision="REJECT", reasons=reasons)
    return PreTradeRiskResult(decision="PASS")
