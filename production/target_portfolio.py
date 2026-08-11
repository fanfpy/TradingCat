#!/usr/bin/env python3
"""
TargetPortfolio — 目标组合构建（架构 v4.0 决策层）
====================================================
PositionIntent[] → TargetPortfolio → PortfolioRisk 审查 → 最终目标仓位。

管线职责：
1. 从 PositionIntent[] 构建 PositionPlan[]（转译 target_fraction → PortfolioRisk 可消费的格式）
2. 合并现有持仓（is_proposed=False）与拟议新仓（is_proposed=True）
3. 调用 PortfolioRisk.check_portfolio 做组合审查
4. 审查通过 → 返回最终目标组合；失败 → 拟议新仓归零，保留现有持仓

架构约束：
- TargetPortfolio 不持有资金授权——最终执行仍需 ExecutionPlan + Human Confirmation
- 单向依赖：position.py → target_portfolio.py → portfolio_risk.py
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from production.position import PositionIntent
from production.portfolio_risk import PositionPlan, PortfolioCheck, apply_portfolio_decision
from shared import db as dbm
from shared.security import require_security_metadata


def _risk_metadata(conn, symbol: str) -> Dict:
    security = require_security_metadata(conn, symbol)
    bars = dbm.get_bars(conn, symbol)
    dollar = sorted(float(row["close"]) * float(row["volume"])
                    for row in bars[-60:] if row["close"] and row["volume"])
    return {
        "sector": security["sector"], "currency": security["currency"],
        "asset_type": security["asset_type"], "beta": float(security["beta"]),
        "leverage": float(security["leverage"]),
        "median_dollar_volume": dollar[len(dollar) // 2] if dollar else None,
    }


@dataclass
class TargetPortfolio:
    """目标组合：PositionIntent[] 经组合审查后的最终仓位计划。

    Attributes:
        intents: 原始 PositionIntent[]（PositionSizer 输出）
        final_fracs: {symbol: target_fraction} 审查后最终目标仓位
        passed: 组合审查是否通过
        failures: 审查失败原因列表
        details: 审查详情
    """
    intents: List[PositionIntent] = field(default_factory=list)
    final_fracs: Dict[str, float] = field(default_factory=dict)
    passed: bool = False
    failures: List[str] = field(default_factory=list)
    details: Dict = field(default_factory=dict)


def build_target_portfolio(
    conn,
    equity: float,
    intents: List[PositionIntent],
    existing_positions: Optional[List[PositionPlan]] = None,
    account_state: Optional[object] = None,
    policy: Optional[Dict] = None,
) -> TargetPortfolio:
    """从 PositionIntent[] 构建目标组合，经 PortfolioRisk 审查。

    Args:
        conn: StateRepository 连接
        equity: 账户权益
        intents: PositionSizer 输出的仓位意图列表
        existing_positions: 现有持仓（PositionPlan[], is_proposed=False）
        account_state: AccountState（D-7 购买力检查）

    Returns:
        TargetPortfolio 含审查结果与最终目标仓位
    """
    # 1. 将 PositionIntent 转译为最终目标。相同 symbol 的 intent 替换现有目标，
    # 而不是与现有持仓相加；SELL(target=0) 也必须保留，供后续生成平仓差额。
    existing_by_symbol = {p.symbol: p for p in (existing_positions or [])}
    proposed_by_symbol: Dict[str, PositionPlan] = {}
    intent_by_symbol: Dict[str, PositionIntent] = {}
    for intent in intents:
        intent_by_symbol[intent.symbol] = intent
        metadata = _risk_metadata(conn, intent.symbol)
        proposed_by_symbol[intent.symbol] = PositionPlan(
            symbol=intent.symbol,
            target_frac=max(0.0, intent.target_fraction),
            stop_price=intent.stop_price,
            entry_price=intent.entry_price,
            is_proposed=True,
            **metadata,
        )

    # 2. 未发生变化的现有持仓 + 每个 symbol 的新目标（无重复 symbol）。
    all_positions = [p for p in existing_by_symbol.values()
                     if p.symbol not in proposed_by_symbol]
    all_positions.extend(proposed_by_symbol.values())

    if not all_positions:
        return TargetPortfolio(intents=intents, passed=True,
                               final_fracs={}, details={"reason": "无仓位"})

    # 3. 组合审查（含 AccountState 购买力检查）
    result = apply_portfolio_decision(
        conn, equity, all_positions, account_state=account_state, policy=policy)
    if not result["passed"]:
        # 风控失败时新增/加仓回退到当前目标，但减仓/清仓始终允许继续降风险。
        for symbol, intent in intent_by_symbol.items():
            current = existing_by_symbol.get(symbol)
            current_frac = current.target_frac if current is not None else 0.0
            if intent.side == "SELL" and intent.target_fraction <= current_frac:
                result["final_fracs"][symbol] = max(0.0, intent.target_fraction)
            else:
                result["final_fracs"][symbol] = current_frac

    return TargetPortfolio(
        intents=intents,
        final_fracs=result["final_fracs"],
        passed=result["passed"],
        failures=result["failures"],
        details=result["details"],
    )


# ────────────────────────────────────────────────────────────────
# 冒烟测试
# ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from shared import db as dbm
    from production.position import KellyPositionSizer

    conn = dbm.get_core_conn(":memory:")

    # 造两标的 252+ 根日线（高相关 → 组合审查会限制对敞口）
    import math
    for sym, base in [("A.US", 100.0), ("B.US", 200.0)]:
        rows = []
        for i in range(300):
            c = base + 2.0 * math.sin(i / 8) + i * 0.05
            rows.append({"ts": f"2023-{(i//28)+1:02d}-{(i%28)+1:02d}",
                         "open": c-0.2, "high": c+1.0, "low": c-1.0, "close": c, "volume": 20_000_000})
        dbm.upsert_bars(conn, sym, rows, "test")

    # case1: 两个 PositionIntent → TargetPortfolio → 组合审查
    intents = [
        PositionIntent(symbol="A.US", side="BUY", target_fraction=0.08,
                       entry_price=100.0, stop_price=95.0, rationale="test A"),
        PositionIntent(symbol="B.US", side="BUY", target_fraction=0.08,
                       entry_price=200.0, stop_price=190.0, rationale="test B"),
    ]
    tp = build_target_portfolio(conn, 100_000, intents)
    print(f"case1: passed={tp.passed} failures={tp.failures}")
    print(f"  final_fracs={tp.final_fracs}")
    # A+B 高相关 16% > 15% → 应失败，拟议新仓归零
    assert not tp.passed, "高相关对 16% 应失败"
    assert tp.final_fracs["A.US"] == 0.0 and tp.final_fracs["B.US"] == 0.0

    # case2: 降低仓位到 7%+7% → 14% ≤ 15% 应通过
    intents2 = [
        PositionIntent(symbol="A.US", side="BUY", target_fraction=0.07,
                       entry_price=100.0, stop_price=95.0, rationale="test A"),
        PositionIntent(symbol="B.US", side="BUY", target_fraction=0.07,
                       entry_price=200.0, stop_price=190.0, rationale="test B"),
    ]
    tp2 = build_target_portfolio(conn, 100_000, intents2)
    print(f"case2: passed={tp2.passed} failures={tp2.failures}")
    assert tp2.passed, "高相关对 14% 应通过"
    assert tp2.final_fracs["A.US"] == 0.07

    # case3: 合并现有持仓
    existing = [PositionPlan("C.US", 0.05, stop_price=None, entry_price=None,
                             is_proposed=False, risk_group="existing")]
    tp3 = build_target_portfolio(conn, 100_000, intents2, existing_positions=existing)
    print(f"case3: passed={tp3.passed} final_fracs={tp3.final_fracs}")
    assert tp3.passed
    assert tp3.final_fracs["C.US"] == 0.05  # 现有持仓保留

    # case4: 空 intents → 直接通过
    tp4 = build_target_portfolio(conn, 100_000, [])
    assert tp4.passed and tp4.final_fracs == {}

    print("target_portfolio.py 冒烟测试通过 ✅")
