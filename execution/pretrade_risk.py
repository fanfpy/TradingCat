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

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional

from execution.models import ExecutionPlan, Confirmation, MarketState, PreTradeRiskResult, now_utc, parse_ts

MAX_PENDING_INTENTS = 10  # 同一账户最多挂单数（防失控堆积）


@dataclass(frozen=True)
class RiskLimits:
    """提交前硬限额；未配置的限额不会被猜测，但已配置的状态缺失会拒绝。"""

    max_order_notional: Optional[float] = None
    max_order_notional_fraction: Optional[float] = None
    max_position_notional: Optional[float] = None
    max_position_notional_fraction: Optional[float] = None
    max_portfolio_notional: Optional[float] = None
    max_portfolio_notional_fraction: Optional[float] = None
    max_daily_loss: Optional[float] = None
    max_daily_loss_fraction: Optional[float] = None

    @classmethod
    def from_mapping(cls, values: Optional[Mapping]) -> "RiskLimits":
        if values is None:
            return cls()
        aliases = {
            "max_single_order_notional": "max_order_notional",
            "max_single_order_notional_fraction": "max_order_notional_fraction",
            "max_single_position_notional": "max_position_notional",
            "max_single_position_notional_fraction": "max_position_notional_fraction",
        }
        data = {aliases.get(k, k): v for k, v in dict(values).items()}
        fields = cls.__dataclass_fields__
        return cls(**{k: data[k] for k in fields if k in data})


def _configured_limits(limits: Optional[object]) -> Optional[RiskLimits]:
    if limits is None:
        return None
    return limits if isinstance(limits, RiskLimits) else RiskLimits.from_mapping(limits)


def _limit_value(absolute: Optional[float], fraction: Optional[float], nav: Optional[float]) -> Optional[float]:
    if absolute is not None:
        return float(absolute)
    if fraction is not None and nav is not None:
        return float(fraction) * nav
    return None


def evaluate(plan: ExecutionPlan, confirmation: Confirmation,
             account_state, market_states: Dict[str, MarketState],
             pending_intents: int = 0, unknown_intents: int = 0,
             risk_limits: Optional[object] = None,
             daily_loss: Optional[float] = None) -> PreTradeRiskResult:
    """执行 PreTradeRisk 校验。只返回 PASS/REJECT + 原因，绝不修改任何字段。

    unknown_intents: 处于 UNKNOWN 状态的现有订单数（D-9：订单状态 UNKNOWN 时禁止新订单）。
    """
    reasons: List[str] = []
    limits = _configured_limits(risk_limits)

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
    if confirmation.is_expired():
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

    # 9b. SELL 不能凭空制造仓位；AccountState 缺失或持仓快照不含足量仓位时拒绝。
    if account_state is not None:
        available_qty: Dict[str, float] = {}
        for position in (getattr(account_state, "positions", None) or []):
            symbol = str(position.get("symbol", ""))
            available_qty[symbol] = available_qty.get(symbol, 0.0) + float(
                position.get("quantity", 0) or 0)
        for order in plan.orders:
            if order.side == "SELL" and available_qty.get(order.symbol, 0.0) + 1e-9 < order.quantity:
                reasons.append(
                    f"卖出仓位不足:{order.symbol} {order.quantity}>"
                    f"{available_qty.get(order.symbol, 0.0)}")

    # 10. 可选但严格的资金/仓位/损失限额。调用方一旦配置限额，
    # 缺少 NAV、持仓价格或日损失快照都必须拒绝，不能以默认值放行。
    if limits is not None and account_state is not None:
        nav = getattr(account_state, "nav", None)
        if nav is None or float(nav) <= 0:
            nav = getattr(account_state, "cash", None)
        nav = float(nav) if nav is not None and float(nav) > 0 else None
        has_fraction_limit = any(getattr(limits, name) is not None for name in (
            "max_order_notional_fraction", "max_position_notional_fraction",
            "max_portfolio_notional_fraction", "max_daily_loss_fraction"))
        if has_fraction_limit and nav is None:
            reasons.append("风险限额需要有效 NAV/现金快照")

        notionals = []
        for o in plan.orders:
            ms = market_states.get(o.symbol)
            if ms is not None and ms.price > 0:
                notionals.append((o, float(o.quantity) * float(ms.price)))
        if len(notionals) != len(plan.orders):
            reasons.append("风险限额无法计算全部订单名义金额")

        order_limit = _limit_value(limits.max_order_notional,
                                   limits.max_order_notional_fraction, nav)
        if order_limit is not None:
            if order_limit < 0:
                reasons.append("单笔限额不能为负")
            for o, notional in notionals:
                if notional > order_limit + 1e-9:
                    reasons.append(f"单笔名义超限:{o.symbol} {notional:.2f}>{order_limit:.2f}")

        positions = getattr(account_state, "positions", None)
        if positions is None:
            positions = []
        current_qty: Dict[str, float] = {}
        current_gross = 0.0
        position_prices_known = True
        for position in positions:
            symbol = str(position.get("symbol", ""))
            qty = float(position.get("quantity", 0) or 0)
            ms = market_states.get(symbol)
            price = (float(ms.price) if ms is not None and ms.price > 0
                     else float(position.get("last_price", 0) or 0))
            if symbol and qty != 0:
                current_qty[symbol] = current_qty.get(symbol, 0.0) + qty
                if price <= 0:
                    position_prices_known = False
                else:
                    current_gross += abs(qty) * price
        for o, _ in notionals:
            current_qty[o.symbol] = current_qty.get(o.symbol, 0.0) + (
                o.quantity if o.side == "BUY" else -o.quantity)

        position_limit = _limit_value(limits.max_position_notional,
                                       limits.max_position_notional_fraction, nav)
        portfolio_limit = _limit_value(limits.max_portfolio_notional,
                                       limits.max_portfolio_notional_fraction, nav)
        if position_limit is not None or portfolio_limit is not None:
            projected_gross = 0.0
            for symbol, qty in current_qty.items():
                ms = market_states.get(symbol)
                if ms is None or ms.price <= 0:
                    position = next((p for p in positions if p.get("symbol") == symbol), {})
                    price = float(position.get("last_price", 0) or 0)
                else:
                    price = float(ms.price)
                if qty != 0 and price <= 0:
                    position_prices_known = False
                    continue
                projected = abs(qty) * price
                projected_gross += projected
                if position_limit is not None and projected > position_limit + 1e-9:
                    reasons.append(f"单仓名义超限:{symbol} {projected:.2f}>{position_limit:.2f}")
            if not position_prices_known:
                reasons.append("风险限额无法计算全部持仓名义金额")
            if portfolio_limit is not None and projected_gross > portfolio_limit + 1e-9:
                reasons.append(f"组合名义超限:{projected_gross:.2f}>{portfolio_limit:.2f}")

        daily_limit = _limit_value(limits.max_daily_loss,
                                   limits.max_daily_loss_fraction, nav)
        if daily_limit is not None:
            if daily_limit < 0:
                reasons.append("日损失限额不能为负")
            elif daily_loss is None:
                reasons.append("日损失快照缺失，禁止提交")
            elif float(daily_loss) > daily_limit + 1e-9:
                reasons.append(f"日损失超限:{float(daily_loss):.2f}>{daily_limit:.2f}")

    if reasons:
        return PreTradeRiskResult(decision="REJECT", reasons=reasons)
    return PreTradeRiskResult(decision="PASS")
