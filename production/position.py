#!/usr/bin/env python3
"""
收缩 Kelly 仓位 + 止损风险仓位 — 交易系统 v4.0（架构 D-2/D-10）
============================================================
spec §3.2-3.3：

Kelly（只用所有 OOS 折聚合后的交易统计）：
    p = OOS 胜率
    b = OOS 平均盈利 / OOS 平均亏损
    rawKelly = p - (1-p)/b
    reliability = n / (n + 20)
    shrunkKelly = max(0, rawKelly) × reliability

证据分级：
    n < 12        → 0（不建仓）
    12 <= n < 20  → shrunkKelly × 1/8
    n >= 20       → shrunkKelly × 1/4
正收益 OOS 折 < 3/4 → 仓位同样为 0

止损风险仓位：
    stopDistance = (entryPrice - stopPrice) / entryPrice
    riskBasedFraction = 0.5% / stopDistance
    targetFraction = min(fractionalKelly, riskBasedFraction, 10%)

v4.0 抽象（D-2/D-10）：
- PositionSizer 是协议接口，KellyPositionSizer 是现有收缩 Kelly 的实现
- PositionIntent 是 data contract（非独立 service）：PositionSizer → PositionIntent[] → TargetPortfolio
- 现有函数（kelly_fraction/target_fraction/...）保留为底层工具，供 KellyPositionSizer 内部复用
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, runtime_checkable


# ────────────────────────────────────────────────────────────────
# v4.0 PositionSizer 抽象（D-2 / D-10）
# ────────────────────────────────────────────────────────────────

@dataclass
class PositionIntent:
    """PositionSizer 输出 data contract（D-10 lineage 一环）。

    含义：策略信号 X 在账户状态 Y 下，希望建立的方向与目标仓位。
    不携带资金授权——最终执行必须经过 ExecutionPlan + Human Confirmation。
    """
    symbol: str
    side: str                                   # BUY|SELL
    target_fraction: float                      # 目标仓位（0..1 权益）
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_quantity: Optional[int] = None       # 计算得出（equity × fraction / entry_price）
    rationale: str = ""                         # 可审计理由（为什么是这个仓位）
    evidence: Dict = field(default_factory=dict)  # Kelly 统计等证据快照


@runtime_checkable
class PositionSizer(Protocol):
    """仓位决策接口。输入信号与账户状态，输出 PositionIntent[]。"""

    def size(self, signal: Dict, equity: float,
             account_state: Optional[object] = None) -> List[PositionIntent]:
        ...


class KellyPositionSizer:
    """现有收缩 Kelly + 止损风险仓位的实现（spec §3.2-3.3）。

    signal 期望字段：
        symbol: str
        oos_stats: Dict   （aggregate_oos_trades 输出）
        entry_price: float
        stop_price: float
    """

    def __init__(self, policy: Optional[Dict] = None):
        self.policy = dict(policy or {})

    def size(self, signal: Dict, equity: float,
             account_state: Optional[object] = None) -> List[PositionIntent]:
        symbol = signal["symbol"]
        stats = signal["oos_stats"]
        entry = signal.get("entry_price")
        stop = signal.get("stop_price")
        if stats.get("final_test_accepted") is not True:
            return [PositionIntent(
                symbol=symbol, side="BUY", target_fraction=0.0,
                entry_price=entry, stop_price=stop, target_quantity=0,
                rationale="最终 Holdout 未验收，Kelly 禁止消费该统计",
                evidence={"oos_stats": stats, "executable": False},
            )]
        if not entry:
            return [PositionIntent(symbol=symbol, side="BUY", target_fraction=0.0,
                                   rationale="无入场价，仓位=0")]
        kf = kelly_fraction(stats)
        tf = target_fraction(
            kf, entry, stop,
            risk_per_trade=float(self.policy.get("risk_per_trade", RISK_PER_TRADE)),
            max_single_position=float(
                self.policy.get("max_single_position", MAX_SINGLE_POSITION)),
        )
        qty = target_quantity(equity, tf, entry) if tf > 0 else 0
        intent = PositionIntent(
            symbol=symbol, side="BUY", target_fraction=tf,
            entry_price=entry, stop_price=stop, target_quantity=qty,
            rationale=f"kelly={kf:.4f} target_frac={tf:.4f}",
            evidence={"oos_stats": stats},
        )
        return [intent]


# ────────────────────────────────────────────────────────────────
# Kelly（底层工具）
# ────────────────────────────────────────────────────────────────

def aggregate_oos_trades(folds) -> Dict:
    """兼容入口；实现位于 research 层，避免 research 反向依赖 production。"""
    from research.walk_forward import aggregate_oos_trades as _aggregate
    return _aggregate(folds)


def kelly_fraction(stats: Dict) -> float:
    """收缩 Kelly 仓位比例（0..1）。"""
    n = stats["n"]
    if n < 12:
        return 0.0
    if stats["positive_folds"] / max(stats["total_folds"], 1) < 0.75:
        return 0.0

    p = stats["p"]
    b = stats["b"]
    if b <= 0:
        return 0.0
    raw_kelly = p - (1 - p) / b
    reliability = n / (n + 20)
    shrunk = max(0.0, raw_kelly) * reliability

    if n < 20:
        return shrunk * (1 / 8)
    return shrunk * (1 / 4)


# ────────────────────────────────────────────────────────────────
# 止损风险仓位 + 最终目标
# ────────────────────────────────────────────────────────────────

RISK_PER_TRADE = 0.005      # 0.5% 权益风险/单标的
MAX_SINGLE_POSITION = 0.10  # 单标的上限 10%


def risk_based_fraction(entry_price: float, stop_price: float,
                        risk_per_trade: float = RISK_PER_TRADE) -> float:
    """止损距离换算为风险仓位。"""
    if entry_price <= 0:
        return 0.0
    stop_distance = (entry_price - stop_price) / entry_price
    if stop_distance <= 0:
        return 0.0
    return risk_per_trade / stop_distance


def target_fraction(fractional_kelly: float, entry_price: float, stop_price: float,
                    risk_per_trade: float = RISK_PER_TRADE,
                    max_single_position: float = MAX_SINGLE_POSITION) -> float:
    """最终单标的目标仓位 = min(Kelly, 风险仓位, 10%)。"""
    rb = risk_based_fraction(entry_price, stop_price, risk_per_trade)
    return min(fractional_kelly, rb, max_single_position)


def target_quantity(equity: float, target_frac: float, entry_price: float) -> int:
    """quantity = floor(targetNotional / entryPrice)。"""
    target_notional = equity * target_frac
    return int(target_notional // entry_price) if entry_price > 0 else 0


# ────────────────────────────────────────────────────────────────
# 冒烟测试
# ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from research.walk_forward import FoldResult, WFResult
    from shared.backtest import Trade

    # 构造 4 折，每折 6 笔 OOS 交易（共 24 笔，n>=20 档）
    folds = []
    for f in range(4):
        fr = FoldResult(fold=f + 1, params={}, train_start=0, train_end=100,
                        test_start=100, test_end=200)
        trades = []
        for k in range(6):
            pnl = 12.0 if k < 4 else -6.0  # 66% 胜率
            trades.append(Trade(entry_idx=k, entry_price=100.0, exit_idx=k + 1,
                                exit_price=100.0 * (1 + pnl / 100), pnl_pct=pnl))
        fr.oos_result = type("R", (), {"trades": trades})()
        fr.oos_return_pct = 8.0
        folds.append(fr)

    stats = aggregate_oos_trades(folds)
    print("stats:", {k: round(v, 3) if isinstance(v, float) else v for k, v in stats.items()})
    kf = kelly_fraction(stats)
    print("kelly_fraction:", round(kf, 4))
    assert kf > 0, "24 笔 + 正收益折4/4 应 > 0"

    # n < 12 → 0
    stats2 = dict(stats, n=10, p=0.7, b=2.0)
    assert kelly_fraction(stats2) == 0.0

    # 正收益折 < 3/4 → 0
    stats3 = dict(stats, positive_folds=2, total_folds=4)
    assert kelly_fraction(stats3) == 0.0

    # 风险仓位 + 最终目标
    rb = risk_based_fraction(100.0, 95.0)
    print("risk_based_fraction:", round(rb, 4))
    assert abs(rb - 0.10) < 1e-9  # 5% 距离 → 0.5%/5% = 10%
    tf = target_fraction(0.20, 100.0, 95.0)
    print("target_fraction:", round(tf, 4))
    assert abs(tf - 0.10) < 1e-9  # min(0.2, 0.1, 0.1) = 0.1
    q = target_quantity(100_000, 0.10, 100.0)
    assert q == 100

    # v4.0 PositionSizer 抽象验证
    sizer = KellyPositionSizer()
    intents = sizer.size({"symbol": "TEST.US", "oos_stats": stats,
                          "entry_price": 100.0, "stop_price": 95.0}, equity=100_000)
    assert len(intents) == 1
    it = intents[0]
    assert it.symbol == "TEST.US" and it.side == "BUY"
    assert 0 < it.target_fraction <= 0.10
    assert it.target_quantity == target_quantity(100_000, it.target_fraction, 100.0)
    assert it.rationale
    # 无入场价 → 0 仓位
    intents0 = sizer.size({"symbol": "TEST.US", "oos_stats": stats}, equity=100_000)
    assert intents0[0].target_fraction == 0.0

    print("position.py 冒烟测试通过 ✅（含 v4.0 PositionSizer 抽象）")
