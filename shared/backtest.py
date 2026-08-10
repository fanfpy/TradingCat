#!/usr/bin/env python3
"""
本地回测引擎 — 交易系统 v3.0
==============================
单标的 + 单参数组回测。1620 组参数网格在 research/backtest.py 层循环调用。

核心逻辑（spec §2.3-2.5）：
- 入场模式：pullback / breakout / hybrid
- 止损：入场以来峰值 − ATR倍数 × ATR22（只增不减，只用完成 bar）
- 触发模拟：open < stop → open 退出；否则 stop 退出（不假设跳空成交）
- 成本：每边 25bps（可选）
- STP LMT 压力测试：limit = stop − 1×ATR，跳过限价记未成交
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple, TYPE_CHECKING

from shared.strategy_evaluator import (
    ADX_PERIOD, ADX_THRESHOLD, ATR_PERIOD, ENTRY_MODES, EXIT_MODES,
    MA_CROSS_FAST_DEFAULT, MA_CROSS_SLOW_DEFAULT, MA_SLOPE_PERIOD,
    StrategyEvaluator, dead_cross, entry_rule,
)

if TYPE_CHECKING:
    from shared.alpha_model import AlphaModel

# ────────────────────────────────────────────────────────────────
# 参数定义
# ────────────────────────────────────────────────────────────────

MA_PERIODS = (10, 20, 50, 100, 150, 200)
ATR_MULTIPLES = (2.5, 3.0, 3.5, 4.0, 5.0)
BUFFERS = (0.0, 0.01, 0.02)

PULLBACK_LOOKBACK = 5

# ADX 过滤（用户 2026-08-07 需求：回测引擎增加 ADX 过滤维度）
ADX_FILTER_DEFAULT = False  # 默认关闭，完全向后兼容（1620 组原网格不变）
# 阈值多档（2026-08-08）：不同标的趋势强度不同（实测已入池标的 ADX 中位数
# 21~23，P75 28~30；候选池 100+ 标的差异更大），固定 20 会漏放/误杀。
# 由 WF 训练段在 4 档中逐标的选最优，而不是全局一刀切。
ADX_THRESHOLDS = (15, 20, 25, 30)
# DI 方向确认（2026-08-08）：ADX 只衡量趋势强度、不辨方向——强下跌趋势 ADX
# 照样 40+，只按 ADX>阈值 放行会把"做多"送进下跌趋势。加 +DI > −DI 多头
# 方向确认（多头过滤）堵住该洞。默认关闭，由 ADX 网格统一开启。
ADX_DIRECTION_FILTER_DEFAULT = False

# 死叉出场默认参数（可由 params.ma_cross_fast / params.ma_cross_slow 覆盖）
COST_BPS = 25  # 每边 25bps

# 全参数网格 = 6 入场 × 6 MA × 5 ATR × 3 buffer × 3 exit = 1620 组
PARAM_GRID: List[Dict] = [
    {"entry_mode": m, "ma_period": ma, "atr_multiple": am, "buffer": b, "exit_mode": ex}
    for m in ENTRY_MODES
    for ma in MA_PERIODS
    for am in ATR_MULTIPLES
    for b in BUFFERS
    for ex in EXIT_MODES
]

# ADX 过滤参数网格（2026-08-07 新增，用户需求；2026-08-08 升级多档阈值 + 方向确认）：
# 搜索空间 = 1620 组无 ADX（关） + 1620×4 档阈值（15/20/25/30，开+DI方向确认）= 8100 组。
# 由 WF 训练段为每个标的自主决定：ADX 过滤开还是关、阈值用哪档——因为实测
# GLD 上 ADX 过滤反而有害（86.7%→15.2%），而 TQQQ 上有益（-7.8%→+35.9%），
# 必须让数据选，不能全局一刀切。
PARAM_GRID_ADX: List[Dict] = PARAM_GRID + [
    {**p, "adx_filter": True, "adx_threshold": t, "adx_period": ADX_PERIOD,
     "adx_direction": True}
    for p in PARAM_GRID
    for t in ADX_THRESHOLDS
]


@dataclass
class Trade:
    entry_idx: int
    entry_price: float
    exit_idx: Optional[int] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""
    pnl_pct: float = 0.0


@dataclass
class BacktestResult:
    symbol: str
    params: Dict
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    total_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    sharpe_daily: float = 0.0
    trade_count: int = 0
    kelly: float = 0.0

    # 统计辅助
    def stats(self) -> Dict:
        return {
            "total_return_pct": round(self.total_return_pct, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "win_rate": round(self.win_rate, 3),
            "profit_factor": round(self.profit_factor, 3),
            "sharpe_daily": round(self.sharpe_daily, 3),
            "trade_count": self.trade_count,
            "kelly": round(self.kelly, 4),
        }


# ────────────────────────────────────────────────────────────────
# 入场信号
# ────────────────────────────────────────────────────────────────

def check_entry(closes: List[float], mas: List[float], slopes: List[float],
                i: int, entry_mode: str, buffer: float,
                fast_ma: Optional[List[float]] = None,
                ma_period: int = 20) -> bool:
    """第 i 根完成 bar 是否触发入场（只用完成日线）。

    入场模式（买入信号多样化）：
        pullback        回踩均线（|close-MA|/MA <= buffer 且 MA 斜率上行）
        breakout        先破 MA 再站上（close[i-1]<MA[i-1] 且今日突破/贴近）
        hybrid          贴近均线（|close-MA|/MA <= buffer）
        momentum        趋势追入（close > MA 且 MA 斜率上行即入场，不等回踩）
        donchian        唐奇安突破（close 创过去 ma_period 日新高）
        golden_cross    金叉入场（fast_ma 上穿主均线 mas，与 ma_cross 出场对称）
    """
    return entry_rule(
        closes, mas, slopes, i, entry_mode, buffer,
        fast_ma=fast_ma, ma_period=ma_period,
    )


def cross_dead(fast_ma: List[float], slow_ma: List[float], i: int) -> bool:
    """第 i 根完成 bar 是否触发双均线死叉（fast 下穿 slow）。

    条件：当前 bar 两条 MA 均有效（>0），上一完成 bar 也均有效；
    且 当前 fast < slow 而上一 bar fast >= slow → 死叉信号。
    前导无效区（MA 前 period-1 个为 0）一律不判，避免 fast>=0 误报。
    """
    return dead_cross(fast_ma, slow_ma, i)


# ────────────────────────────────────────────────────────────────
# 主回测
# ────────────────────────────────────────────────────────────────

def run_backtest(
    symbol: str,
    ts: List[str],
    opens: List[float],
    highs: List[float],
    lows: List[float],
    closes: List[float],
    params: Dict,
    cost_bps: float = COST_BPS,
    stp_lmt: bool = True,
    start_idx: int = 0,
    end_idx: Optional[int] = None,
    initial_cash: float = 100_000.0,
    features: Optional[List[Dict]] = None,
    volumes: Optional[List[float]] = None,
    alpha_model: Optional["AlphaModel"] = None,
) -> BacktestResult:
    """运行单参数组回测。

    Args:
        ts/opens/highs/lows/closes: 等长序列（已按日期升序）
        params: {entry_mode, ma_period, atr_multiple, buffer, exit_mode, ...}
        cost_bps: 每边成本（bps）
        stp_lmt: 是否启用 STP LMT 压力测试
        start_idx/end_idx: 回测区间（用于 walk-forward 训练/测试段）

    exit_mode 取值（EXIT_MODES）：
        chandelier          默认，ATR 吊灯止损（原逻辑）
        ma_cross            双均线死叉出场（MA_fast 下穿 MA_slow，默认 20/100），不设 ATR 止损
        chandelier_or_cross 任一先触发
    """
    n = len(closes)
    if end_idx is None:
        end_idx = n

    evaluator = StrategyEvaluator(opens, highs, lows, closes, params)

    # v4 决策链：OHLCV -> FeatureLab -> AlphaModel -> BacktestEngine。
    # Walk-Forward 会从 NativeBacktestEngine 传入一次性预计算的 features；独立
    # 调用 run_backtest 时则在此自动计算，保持旧 API 可用。
    if features is None:
        from shared.feature_lab import compute_features
        features = compute_features(closes, highs, lows, volumes)
    if alpha_model is None:
        from shared.alpha_model import RuleBasedAlpha
        alpha_model = RuleBasedAlpha(
            evaluator.entry_mode, evaluator.buffer, evaluator.ma_period)
    alpha_signals = alpha_model.generate(
        symbol, closes, evaluator.mas, evaluator.slopes,
        fast_ma=evaluator.fast_ma or None,
        features=features, start_idx=max(1, start_idx), end_idx=end_idx,
    )
    alpha_entry_indices = {signal.entry_idx for signal in alpha_signals}

    trades: List[Trade] = []
    equity: List[float] = []
    cash = initial_cash
    position = 0.0  # 持有数量（份额）
    entry_price = 0.0
    peak_high = 0.0
    entry_idx = -1
    open_trade: Optional[Trade] = None
    exit_now: Optional[Tuple[float, str]] = None  # (exit_price, reason) 本日退出

    for i in range(max(1, start_idx), end_idx):
        exit_now = None

        if entry_idx == -1:
            # 空仓 → 找入场
            entry_decision = evaluator.evaluate_entry(
                i, candidate_override=i in alpha_entry_indices)
            if entry_decision.triggered:
                px = closes[i]
                entry_cost = px * (1 + cost_bps / 10_000)
                position = cash / entry_cost
                entry_price = px
                peak_high = highs[i]
                entry_idx = i
                open_trade = Trade(entry_idx=i, entry_price=px)
                cash = 0.0
        else:
            # 持仓 → 维护入场以来峰值（只用已完成 bar：highs[i-1]）
            # （ma_cross 形态下 peak_high 不参与退出，保留赋值仅统一结构）
            if highs[i - 1] > peak_high:
                peak_high = highs[i - 1]

            exit_decision = evaluator.evaluate_exit(i, peak_high)
            if exit_decision.triggered:
                if exit_decision.reason in ("stop", "gap_open") and stp_lmt:
                    stop = float(exit_decision.stop_price)
                    limit = stop - evaluator.atr[i]
                    if lows[i] <= limit:
                        exit_now = (min(opens[i], limit), "stp_lmt_fill")
                    else:
                        open_trade.exit_reason = "stp_lmt_unfilled"
                else:
                    exit_now = (
                        float(exit_decision.reference_price), exit_decision.reason)

            # 处理退出
            if exit_now is not None:
                exit_px, exit_reason = exit_now
                exit_cost = exit_px * (1 - cost_bps / 10_000)
                cash = position * exit_cost
                open_trade.exit_idx = i
                open_trade.exit_price = exit_px
                open_trade.exit_reason = exit_reason
                open_trade.pnl_pct = (exit_px - entry_price) / entry_price * 100
                trades.append(open_trade)

                entry_idx = -1
                position = 0.0
                open_trade = None

        # 盯市净值（完成 bar 收盘）— 始终记录
        if entry_idx != -1:
            equity.append(cash + position * closes[i])
        else:
            equity.append(cash)

    # 回测结束时仍持仓：按最后收盘价平仓结算（关键修复：避免 OOS 窗口切在主升段
    # 中间时把"持有中的盈利"当成 0 笔 0 收益丢弃——低频死叉形态常跨窗口持有）
    if open_trade is not None and end_idx > 0:
        last_i = end_idx - 1
        exit_px = closes[last_i]
        exit_cost = exit_px * (1 - cost_bps / 10_000)
        cash = position * exit_cost
        open_trade.exit_idx = last_i
        open_trade.exit_price = exit_px
        open_trade.exit_reason = "end_of_window"
        open_trade.pnl_pct = (exit_px - entry_price) / entry_price * 100
        trades.append(open_trade)
        if equity:
            equity[-1] = cash  # 结算后净值 = 现金（已扣成本）
        open_trade = None
        position = 0.0

    result = _compute_stats(symbol, params, trades, equity, closes, start_idx, end_idx)
    return result


def _compute_stats(symbol: str, params: Dict, trades: List[Trade],
                   equity: List[float], closes: List[float],
                   start_idx: int, end_idx: int) -> BacktestResult:
    r = BacktestResult(symbol=symbol, params=params, trades=trades, equity_curve=equity)
    r.trade_count = len(trades)
    if not trades or not equity:
        return r

    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct < 0]
    r.win_rate = len(wins) / len(trades) if trades else 0.0
    gross_win = sum(t.pnl_pct for t in wins)
    gross_loss = abs(sum(t.pnl_pct for t in losses))
    r.profit_factor = gross_win / gross_loss if gross_loss > 0 else (gross_win if gross_win > 0 else 0.0)

    # 总收益：从 equity 序列计算
    if equity:
        base = equity[0]
        r.total_return_pct = (equity[-1] - base) / base * 100 if base else 0.0
        peak = equity[0]
        max_dd = 0.0
        for v in equity:
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
        r.max_drawdown_pct = max_dd * 100

        # 日频 Sharpe（无风险利率 0）
        if len(equity) > 1:
            rets = [(equity[k] - equity[k - 1]) / equity[k - 1] for k in range(1, len(equity)) if equity[k - 1] > 0]
            if len(rets) > 1:
                mean = sum(rets) / len(rets)
                var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
                std = var ** 0.5
                r.sharpe_daily = mean / std if std > 0 else 0.0

    # Kelly（简化）：用盈亏比和胜率
    if losses:
        avg_win = gross_win / len(wins) if wins else 0.0
        avg_loss = gross_loss / len(losses)
        b = avg_win / avg_loss if avg_loss > 0 else 0.0
        p = r.win_rate
        r.kelly = p - (1 - p) / b if b > 0 else 0.0
    return r


# ────────────────────────────────────────────────────────────────
# 冒烟测试
# ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 构造带波动的上升趋势数据，末尾 60 根大幅回撤（保证止损触发）
    import math
    n = 400
    ts = [f"2023-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}" for i in range(n)]
    closes = [100 * (1.0008 ** i) + 3.0 * math.sin(i / 12) for i in range(n - 60)]
    closes += [closes[-1] * (1 - 0.015 * k) for k in range(1, 61)]  # 末尾回撤
    highs = [c + 1.5 for c in closes]
    lows = [c - 1.5 for c in closes]
    opens = [c - 0.3 for c in closes]

    params = {"entry_mode": "hybrid", "ma_period": 50, "atr_multiple": 3.0, "buffer": 0.01}
    r = run_backtest("TEST.US", ts, opens, highs, lows, closes, params)
    print("trades:", r.trade_count, "return:", round(r.total_return_pct, 2),
          "dd:", round(r.max_drawdown_pct, 2), "PF:", round(r.profit_factor, 2),
          "kelly:", round(r.kelly, 4))
    assert r.trade_count >= 1, "趋势数据应产生交易"

    # 无 exit_mode 字段 → 默认 chandelier；显式 chandelier 应结果完全一致（向后兼容）
    r_default = run_backtest("TEST.US", ts, opens, highs, lows, closes, params)
    r_chand = run_backtest("TEST.US", ts, opens, highs, lows, closes, dict(params, exit_mode="chandelier"))
    assert r_default.trade_count == r_chand.trade_count
    assert r_default.total_return_pct == r_chand.total_return_pct

    # 三种出场形态都跑一遍（不崩 + ma_cross 末尾回撤应触发死叉退出）
    for ex in EXIT_MODES:
        r2 = run_backtest("TEST.US", ts, opens, highs, lows, closes, dict(params, exit_mode=ex))
        assert r2.trade_count >= 0
    r_cross = run_backtest("TEST.US", ts, opens, highs, lows, closes,
                           dict(params, exit_mode="ma_cross"))
    assert r_cross.trade_count >= 1, "末尾大幅回撤应触发双均线死叉退出"
    assert all(t.exit_reason == "ma_cross" for t in r_cross.trades), "ma_cross 形态退出原因应为 ma_cross"

    # 参数网格规模检查
    assert len(PARAM_GRID) == 1620, f"参数网格应为 1620 组，实际 {len(PARAM_GRID)}"
    print("backtest.py 冒烟测试通过 ✅ (1620 组参数网格)")
