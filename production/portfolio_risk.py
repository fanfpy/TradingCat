#!/usr/bin/env python3
"""
全组合风控 — 交易系统 v3.0
==========================
spec §3.4：单标的 Kelly 结果不是最终仓位。所有现有持仓 + 所有拟议新仓放入同一个组合计划。

硬门槛：
- 总名义仓位 <= 25% 权益
- 总止损风险 <= 1.5% 权益
- 单风险组敞口 <= 15% 权益
- 任意高相关标的对敞口 <= 15% 权益

参数：
- 高相关阈值 = 0.75
- 相关性窗口 = 252 日
- 最少共同观测 = 200 日

失败语义：任一检查失败 → 组合审查失败，所有拟议新仓仓位 = 0。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from shared import db as dbm

MAX_GROSS_NOTIONAL = 0.25   # 总名义仓位
MAX_STOP_RISK = 0.015       # 总止损风险
MAX_GROUP_EXPOSURE = 0.15   # 单风险组
MAX_PAIR_EXPOSURE = 0.15    # 高相关对
CORR_THRESHOLD = 0.75
CORR_WINDOW = 252
MIN_COMMON_OBS = 200


@dataclass
class PositionPlan:
    symbol: str
    target_frac: float          # 单标的目标仓位（0..1 权益）
    stop_price: Optional[float]
    entry_price: Optional[float]
    risk_group: str = "default"
    is_proposed: bool = True    # True=拟议新仓, False=现有持仓
    sector: str = "UNKNOWN"
    currency: str = "UNKNOWN"
    asset_type: str = "EQUITY"
    beta: float = 1.0
    leverage: float = 1.0
    median_dollar_volume: Optional[float] = None
    event_risk: bool = False

    @property
    def stop_risk_frac(self) -> float:
        """该标的的止损风险（权益比例）。"""
        if not self.entry_price or not self.stop_price or self.entry_price <= 0:
            return self.target_frac * 0.02  # 无法计算止损 → 保守按 2% 止损距
        dist = (self.entry_price - self.stop_price) / self.entry_price
        if dist <= 0:
            return 0.0
        return self.target_frac * dist


@dataclass
class PortfolioCheck:
    passed: bool
    failures: List[str] = field(default_factory=list)
    details: Dict = field(default_factory=dict)


def compute_pair_correlations(conn, symbols: List[str]) -> Dict[tuple, float]:
    """计算标的两两日收益相关性（252 日窗口，最少 200 共同观测）。

    实现要点：按交易日历（ts）对齐而非按索引对齐——跨市场标的（如 HK/US）
    交易日历不同，按索引对齐会将不同日期的收益配对，导致相关性计算错误。
    """
    corr: Dict[tuple, float] = {}
    # 构建 {symbol: {ts: close}} 映射，仅取最后 252 根日线
    ts_close_map: Dict[str, Dict[str, float]] = {}
    for s in symbols:
        bars = dbm.get_bars(conn, s)
        if len(bars) < CORR_WINDOW:
            continue
        ts_close_map[s] = {b["ts"]: float(b["close"]) for b in bars[-CORR_WINDOW:]}

    syms = list(ts_close_map.keys())
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            a, b = syms[i], syms[j]
            # 按 ts 取交集（共同交易日），再按日期升序对齐
            common_ts = sorted(
                set(ts_close_map[a].keys()) & set(ts_close_map[b].keys())
            )
            if len(common_ts) < MIN_COMMON_OBS:
                continue
            ca = [ts_close_map[a][ts] for ts in common_ts]
            cb = [ts_close_map[b][ts] for ts in common_ts]
            # 在相同日期序列上同步计算日收益率：两标的前收盘都 > 0 才纳入
            # （避免 0 或缺失收盘污染收益序列；同一日两边都需要有效前值）
            ra, rb = [], []
            for k in range(1, len(common_ts)):
                if ca[k - 1] > 0 and cb[k - 1] > 0:
                    ra.append(ca[k] / ca[k - 1] - 1)
                    rb.append(cb[k] / cb[k - 1] - 1)
            m = len(ra)
            if m < MIN_COMMON_OBS:
                continue
            ma = sum(ra) / m
            mb = sum(rb) / m
            cov = sum((ra[k] - ma) * (rb[k] - mb) for k in range(m)) / (m - 1)
            va = sum((x - ma) ** 2 for x in ra) / (m - 1)
            vb = sum((x - mb) ** 2 for x in rb) / (m - 1)
            if va > 0 and vb > 0:
                corr[(a, b)] = cov / (va * vb) ** 0.5
    return corr


def check_portfolio(conn, equity: float, positions: List[PositionPlan],
                    account_state: Optional[object] = None,
                    policy: Optional[Dict] = None) -> PortfolioCheck:
    """组合审查。失败 → 所有拟议新仓 = 0（由调用方执行归零）。

    Args:
        equity: 账户权益
        positions: 所有现有持仓 + 拟议新仓
    """
    failures: List[str] = []
    details: Dict = {}
    policy = dict(policy or {})
    max_gross = float(policy.get("max_gross_notional", MAX_GROSS_NOTIONAL))
    max_stop = float(policy.get("max_stop_risk", MAX_STOP_RISK))
    max_group = float(policy.get("max_group_exposure", MAX_GROUP_EXPOSURE))
    max_pair = float(policy.get("max_pair_exposure", MAX_PAIR_EXPOSURE))
    max_sector = float(policy.get("max_sector_exposure", MAX_GROUP_EXPOSURE))
    max_currency = float(policy.get("max_currency_exposure", max_gross))
    max_beta = float(policy.get("max_beta_weighted_exposure", 0.35))
    max_event = float(policy.get("max_event_risk_exposure", 0.10))
    max_adv = float(policy.get("max_adv_participation", 0.05))

    # 1. 总名义仓位
    gross = sum(p.target_frac * max(1.0, p.leverage) for p in positions)
    details["gross_notional_frac"] = gross
    if gross > max_gross:
        failures.append(f"gross_notional>{max_gross*100}%")

    # 2. 总止损风险
    stop_risk = sum(p.stop_risk_frac for p in positions)
    details["stop_risk_frac"] = stop_risk
    if stop_risk > max_stop:
        failures.append(f"stop_risk>{max_stop*100}%")

    # 3. 单风险组敞口（简单：按 risk_group 聚合）
    groups: Dict[str, float] = {}
    for p in positions:
        group = p.risk_group
        if group == "default":
            group = p.sector if p.sector != "UNKNOWN" else p.symbol
        groups[group] = groups.get(group, 0.0) + p.target_frac
    details["group_exposure"] = groups
    for g, exp in groups.items():
        if exp > max_group:
            failures.append(f"group_{g}_exposure>{max_group*100}%")

    # 4. 可解释因子敞口：行业、币种、Beta、事件风险与流动性容量。
    sectors: Dict[str, float] = {}
    currencies: Dict[str, float] = {}
    for p in positions:
        sectors[p.sector] = sectors.get(p.sector, 0.0) + p.target_frac
        currencies[p.currency] = currencies.get(p.currency, 0.0) + p.target_frac
        if p.is_proposed and p.median_dollar_volume and equity > 0:
            participation = p.target_frac * equity / p.median_dollar_volume
            if participation > max_adv:
                failures.append(f"liquidity_{p.symbol}_adv_participation>{max_adv*100}%")
    beta_exposure = sum(p.target_frac * p.beta * p.leverage for p in positions)
    event_exposure = sum(p.target_frac for p in positions if p.event_risk)
    details.update({"sector_exposure": sectors, "currency_exposure": currencies,
                    "beta_weighted_exposure": beta_exposure,
                    "event_risk_exposure": event_exposure})
    for sector, exp in sectors.items():
        if sector != "UNKNOWN" and exp > max_sector:
            failures.append(f"sector_{sector}_exposure>{max_sector*100}%")
    for currency, exp in currencies.items():
        if currency != "UNKNOWN" and exp > max_currency:
            failures.append(f"currency_{currency}_exposure>{max_currency*100}%")
    if beta_exposure > max_beta:
        failures.append(f"beta_weighted_exposure>{max_beta}")
    if event_exposure > max_event:
        failures.append(f"event_risk_exposure>{max_event*100}%")

    # 5. 高相关对敞口
    syms = [p.symbol for p in positions if p.target_frac > 0]
    if len(syms) >= 2:
        corr = compute_pair_correlations(conn, syms)
        details["pairs_checked"] = len(corr)
        for (a, b), c in corr.items():
            if c >= CORR_THRESHOLD:
                exp_a = next((p.target_frac for p in positions if p.symbol == a), 0.0)
                exp_b = next((p.target_frac for p in positions if p.symbol == b), 0.0)
                pair_exp = exp_a + exp_b
                if pair_exp > max_pair:
                    failures.append(f"pair_{a}_{b}_corr{c:.2f}_exp{pair_exp*100:.0f}%")

    # 6. 购买力充足（D-7 AccountState 硬条件接入）
    proposed_buy_notional = sum(
        p.target_frac * equity for p in positions if p.is_proposed and p.target_frac > 0
    )
    if account_state is not None and getattr(account_state, 'synced', False):
        buying_power = getattr(account_state, 'buying_power', None)
        if proposed_buy_notional > 0 and buying_power is None:
            failures.append("buying_power_unknown")
        elif proposed_buy_notional > max(float(buying_power or 0.0), 0.0):
            failures.append(
                f"buying_power_insufficient: need {proposed_buy_notional:.0f} "
                f"have {float(buying_power or 0.0):.0f}")
        pending_orders = getattr(account_state, "open_orders", []) or []
        details["pending_orders"] = len(pending_orders)
        details["buying_power"] = buying_power
        details["account_synced"] = True
    elif account_state is not None:
        details["account_synced"] = False
        failures.append("account_not_synced")
    else:
        details["account_synced"] = False

    return PortfolioCheck(passed=len(failures) == 0, failures=failures, details=details)


def apply_portfolio_decision(conn, equity: float, plans: List[PositionPlan],
                             account_state: Optional[object] = None,
                             policy: Optional[Dict] = None) -> Dict:
    """执行组合审查，输出最终仓位建议。

    Returns:
        {passed, failures, details, final_fracs: {symbol: frac}}
    """
    check = check_portfolio(conn, equity, plans, account_state=account_state,
                            policy=policy)

    final = {}
    if check.passed:
        for p in plans:
            final[p.symbol] = p.target_frac
    else:
        # 组合审查失败 → 所有拟议新仓仓位 = 0（现有持仓保留原样）
        for p in plans:
            final[p.symbol] = p.target_frac if not p.is_proposed else 0.0

    return {
        "passed": check.passed,
        "failures": check.failures,
        "details": check.details,
        "final_fracs": final,
    }


# ────────────────────────────────────────────────────────────────
# 冒烟测试
# ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    conn = dbm.get_core_conn(":memory:")

    # 造两段 252+ 根数据
    import math
    rows_a, rows_b = [], []
    base = 100.0
    for i in range(300):
        c = base + 2.0 * math.sin(i / 8) + i * 0.05
        rows_a.append({"ts": f"2023-{(i//28)+1:02d}-{(i%28)+1:02d}", "open": c-0.2,
                       "high": c+1.0, "low": c-1.0, "close": c, "volume": 20_000_000})
        # B 与 A 高度相关（同走势）
        rows_b.append({"ts": f"2023-{(i//28)+1:02d}-{(i%28)+1:02d}", "open": c*2-0.2,
                       "high": c*2+1.0, "low": c*2-1.0, "close": c*2, "volume": 20_000_000})
    dbm.upsert_bars(conn, "A.US", rows_a, "test")
    dbm.upsert_bars(conn, "B.US", rows_b, "test")

    # 正常组合：A 8% + B 8%（相关 1.0 → 对敞口 16% > 15% 应失败）
    plans = [
        PositionPlan("A.US", 0.08, stop_price=95.0, entry_price=100.0, is_proposed=True),
        PositionPlan("B.US", 0.08, stop_price=190.0, entry_price=200.0, is_proposed=True),
    ]
    r = apply_portfolio_decision(conn, 100_000, plans)
    print("case1 (high-corr pair 16%):", r["passed"], r["failures"])
    assert not r["passed"], "高相关对 16% > 15% 应失败"

    # 归零检查：拟议新仓 → 0
    assert r["final_fracs"]["A.US"] == 0.0 and r["final_fracs"]["B.US"] == 0.0

    # 降低到 7%+7% → 对敞口 14% ≤ 15% 应通过
    plans2 = [
        PositionPlan("A.US", 0.07, stop_price=95.0, entry_price=100.0, is_proposed=True),
        PositionPlan("B.US", 0.07, stop_price=190.0, entry_price=200.0, is_proposed=True),
    ]
    r2 = apply_portfolio_decision(conn, 100_000, plans2)
    print("case2 (high-corr pair 14%):", r2["passed"], r2["failures"])
    assert r2["passed"]

    # case3: 购买力不足应失败
    class FakeAccount:
        synced = True
        buying_power = 5000.0  # 权益 100k 但只 5k 购买力
    plans3 = [
        PositionPlan("A.US", 0.08, stop_price=95.0, entry_price=100.0, is_proposed=True),
    ]
    r3 = apply_portfolio_decision(conn, 100_000, plans3, account_state=FakeAccount())
    print("case3 (buying_power insufficient):", r3["passed"], r3["failures"])
    assert not r3["passed"], "购买力不足应失败"
    print("portfolio_risk.py 冒烟测试通过 ✅")
