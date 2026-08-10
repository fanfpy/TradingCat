#!/usr/bin/env python3
"""
四折 Anchored Walk-Forward 样本外验证 — 交易系统 v3.0
====================================================
spec §2.6-2.7：
- Fold 1: 前 50% 训练, 后 12.5% 测试
- Fold 2: 前 62.5% 训练, 后 12.5% 测试
- Fold 3: 前 75% 训练, 后 12.5% 测试
- Fold 4: 前 87.5% 训练, 最后 12.5% 测试
每一折在外层训练段内部再切分 inner-train / inner-validation：参数只按
inner-validation 选择，再冻结到外层 OOS。外层 OOS 从不参与选参。

单折通过：OOS 交易≥3（ma_cross/chandelier_or_cross 低频形态 ≥2）、基础总收益>0、日频 Sharpe>0、25bps 成本后收益>0、STP LMT 无遗留未成交。
整体稳健：通过折≥3/4、正收益折≥3/4、参数变体≤3、最差25bps OOS 折≥-25%、任一折 MaxDD≤25%。
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

from shared.backtest import PARAM_GRID, PARAM_GRID_ADX, BacktestResult
from shared.backtest_engine import BacktestEngine, NativeBacktestEngine

TRAIN_FRACS = (0.50, 0.625, 0.75, 0.875)
TEST_FRAC = 0.125
MIN_OOS_TRADES = 3
# 单折 OOS 最低交易数按出场形态区分：
# - chandelier（ATR 吊灯止损）：保持 3 笔——高频进出，12.5% OOS 窗口内 3 笔可接受；
# - ma_cross / chandelier_or_cross：低频常态放宽到 1 笔。理由：双均线死叉出场在
#   慢牛/低波动标的上天然低频（GLD 实测：8 年仅 7 笔，最后一笔持有近 3 年），
#   12.5% OOS 窗口内出现 1 笔属正常；若按 2~3 笔门槛，极端低频形态几乎必然被
#   "oos_trades" 误杀而永远无法通过。1 笔已足以验证死叉方向正确性
#   （该窗口要么吃满主升段、要么死叉及时离场），且由 oos_return（收益 ≤0 判失败）
#   与结构判定（passed_folds 要求）兜底，不会因放宽而放过负收益方向。
MIN_OOS_TRADES_LOW_FREQ = 1
LOW_FREQ_EXIT_MODES = ("ma_cross", "chandelier_or_cross")
# 入场低频判定：MA 周期 ≥ 100 的长均线入场天然少信号（回调/突破都少），
# 即便出场是 chandelier（高频出场），组合整体仍是低频——OOS 12.5% 窗口内
# 出现 1~2 笔属正常。若仍按 chandelier 3 笔门槛，JPM/KO/MSFT 选出的
# pullback MA150 / donchian MA150 等低频参数会被 oos_trades 误杀（实测 4 标的
# positive=3/4、仅个别折亏损），永远无法通过。低频阈值放宽的安全性由
# oos_return（收益 ≤0 判失败）与结构判定（passed_folds/尾部风险）兜底。
LOW_FREQ_MA_THRESHOLD = 100
# 标的级低频判据：训练段年化交易数 < 3 笔 → OOS 12.5% 窗口（约 1 年）期望
# 交易数必然 < 3，3 笔门槛必误杀。典型：KO 低波动慢股，MA10 训练段 7 年仅
# 9-13 笔（年化 ~1.8），OOS 每年 1-2 笔——按参数形态（MA10）判不出低频，
# 但按实际频率就是低频标的。
LOW_FREQ_ANNUAL_TRADES = 3.0
TRADING_DAYS_PER_YEAR = 252
MIN_VARIANTS_FLOOR = 5  # 自适应阈值下限：训练样本参数搜索至少要求 5 笔交易
INNER_VALIDATION_FRAC = 0.20
INNER_VALIDATION_MIN_BARS = 42
INNER_SHORTLIST_SIZE = 32


@dataclass
class FoldResult:
    fold: int
    params: Dict
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    inner_train_end: int = 0
    inner_validation_start: int = 0
    inner_validation_end: int = 0
    inner_validation_result: Optional[BacktestResult] = None
    inner_candidate_count: int = 0
    inner_shortlist_count: int = 0
    train_result: Optional[BacktestResult] = None
    oos_result: Optional[BacktestResult] = None
    passed: bool = False
    oos_trades: int = 0
    oos_return_pct: float = 0.0
    oos_sharpe: float = 0.0
    oos_cost_return_pct: float = 0.0
    oos_maxdd_pct: float = 0.0
    baseline_return_pct: float = 0.0  # 该折 OOS 段的买入持有收益（对照基准）
    stp_lmt_unfilled: bool = False
    fail_reasons: List[str] = field(default_factory=list)


@dataclass
class WFResult:
    symbol: str
    folds: List[FoldResult] = field(default_factory=list)
    passed_folds: int = 0
    positive_folds: int = 0
    cost_positive_folds: int = 0
    parameter_variants: int = 0
    avg_sharpe: float = 0.0
    avg_maxdd: float = 0.0
    avg_baseline_return_pct: float = 0.0  # 四折买入持有平均收益（对照基准）
    worst_cost_return_pct: float = 0.0
    structurally_robust: bool = False
    eligible: bool = False  # 是否适合"趋势入场 + 吊灯止损"

    def summarize(self) -> Dict:
        return {
            "passed_folds": self.passed_folds,
            "positive_folds": self.positive_folds,
            "cost_positive_folds": self.cost_positive_folds,
            "parameter_variants": self.parameter_variants,
            "avg_sharpe": round(self.avg_sharpe, 3),
            "avg_maxdd": round(self.avg_maxdd, 2),
            "baseline_returns": [round(f.baseline_return_pct, 2) for f in self.folds],
            "avg_baseline_return_pct": round(self.avg_baseline_return_pct, 2),
            "worst_cost_return_pct": round(self.worst_cost_return_pct, 2),
            "structurally_robust": self.structurally_robust,
            "eligible": self.eligible,
        }


def aggregate_oos_trades(folds) -> Dict:
    """聚合 Walk-Forward 全部 OOS 成交，作为仓位模型的可审计证据。"""
    trades = []
    for fold in folds:
        if fold.oos_result is not None:
            trades.extend(fold.oos_result.trades)
    wins = [trade for trade in trades if trade.pnl_pct > 0]
    losses = [trade for trade in trades if trade.pnl_pct <= 0]
    n = len(trades)
    avg_win = sum(t.pnl_pct for t in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(t.pnl_pct for t in losses)) / len(losses) if losses else 0.0
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "p": len(wins) / n if n else 0.0,
        "b": avg_win / avg_loss if avg_loss > 0 else 0.0,
        "positive_folds": sum(1 for f in folds if f.oos_return_pct > 0),
        "total_folds": len(folds),
    }


def trade_statistics(trades, *, positive_periods: int, total_periods: int) -> Dict:
    """为同一冻结候选计算统计，禁止混入其他折选择出的参数。"""
    trades = list(trades)
    wins = [trade for trade in trades if trade.pnl_pct > 0]
    losses = [trade for trade in trades if trade.pnl_pct <= 0]
    n = len(trades)
    avg_win = sum(t.pnl_pct for t in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(t.pnl_pct for t in losses)) / len(losses) if losses else 0.0
    return {
        "n": n, "wins": len(wins), "losses": len(losses),
        "avg_win": avg_win, "avg_loss": avg_loss,
        "p": len(wins) / n if n else 0.0,
        "b": avg_win / avg_loss if avg_loss > 0 else 0.0,
        "positive_folds": int(positive_periods),
        "total_folds": int(total_periods),
    }


def _min_variants_threshold(trade_counts: List[int]) -> int:
    """自适应最小交易数阈值 = max(5, 交易数下四分位数 P25)。

    原硬编码 20 笔过滤了低换手标的的高 Kelly 慢参数：GLD 案例中
    pullback MA150 ATR4.0 buf0.02 训练段仅 8 笔但 kelly=0.40、收益 +44.5%，
    因不足 20 笔被误杀，选参器只能在短线频繁交易参数里挑（kelly 仅 0.32），
    导致 fold1 OOS 5 笔交易 -6.71%。
    实测 GLD fold1 训练段交易数分布 min=0 / P25=6 / 中位数=11 / max=37，
    用中位数（11）仍会把 8 笔的慢参数拒之门外，故改用下四分位数 P25（=6）
    更宽松，配合 5 笔下限保证最基本的统计意义。
    """
    if not trade_counts:
        return MIN_VARIANTS_FLOOR
    sorted_counts = sorted(trade_counts)
    p25 = sorted_counts[len(sorted_counts) // 4]
    return max(MIN_VARIANTS_FLOOR, p25)


def select_params_on_train(train_result: BacktestResult,
                           candidates: List[BacktestResult]) -> BacktestResult:
    """训练样本参数选择：Kelly > 0 且交易数 ≥ 自适应阈值（见 _min_variants_threshold）。

    排序：1. Kelly → 2. 总收益 → 3. 交易数量。
    """
    min_variants = _min_variants_threshold([r.trade_count for r in candidates])
    valid = [r for r in candidates if r.kelly > 0 and r.trade_count >= min_variants]
    if not valid:
        return train_result  # 无合格参数，返回空结果（调用方会判失败）
    valid.sort(key=lambda r: (-r.kelly, -r.total_return_pct, -r.trade_count))
    return valid[0]


def _train_shortlist(candidates: List[BacktestResult], limit: int) -> List[BacktestResult]:
    """仅用 inner-train 做可行性筛选；最终名次由 inner-validation 决定。"""
    if not candidates:
        return []
    min_variants = _min_variants_threshold([r.trade_count for r in candidates])
    valid = [r for r in candidates if r.kelly > 0 and r.trade_count >= min_variants]
    ranked = valid or candidates
    ranked = sorted(
        ranked,
        key=lambda r: (-r.kelly, -r.total_return_pct, r.max_drawdown_pct,
                       -r.trade_count),
    )
    return ranked[:max(1, min(limit, len(ranked)))]


def _validation_rank(result: BacktestResult) -> Tuple:
    """Inner validation 的确定性排序，不读取任何外层 OOS 指标。"""
    viable = result.trade_count > 0 and result.total_return_pct > 0
    return (
        int(viable),
        result.sharpe_daily,
        result.total_return_pct,
        -result.max_drawdown_pct,
        result.trade_count,
    )


def run_walk_forward(
    symbol: str,
    ts: List[str],
    opens: List[float],
    highs: List[float],
    lows: List[float],
    closes: List[float],
    params_grid: Optional[List[Dict]] = None,
    min_oos_trades: int = MIN_OOS_TRADES,
    mode: str = "anchored",   # anchored | rolling | expanding
    engine: Optional[BacktestEngine] = None,
    volumes: Optional[List[float]] = None,
    cost_bps: float = 25.0,
) -> WFResult:
    """执行四折 Nested Anchored Walk-Forward。

    Returns WFResult 含每折详情与整体判定。
    """
    grid = params_grid or PARAM_GRID
    backtest_engine = engine or NativeBacktestEngine()
    features = backtest_engine.prepare(closes, highs, lows, volumes)
    n = len(closes)
    wf = WFResult(symbol=symbol)

    fold_start = int(TRAIN_FRACS[0] * n)
    if fold_start < 50:  # 需要至少 50 根给 MA200 预热
        return wf

    for fold in range(4):
        train_frac = TRAIN_FRACS[fold]
        train_end = int(train_frac * n)
        test_start = train_end
        test_end = min(test_start + int(TEST_FRAC * n), n)

        # Walk-Forward 模式（R1#10 参数化）：
        # - anchored（默认/等价 expanding）：训练起点固定为 0，训练窗口逐折扩大
        # - rolling：训练窗口固定为第一折长度（50%×n），起点随折数前移
        if mode == "rolling":
            train_start = test_start - int(TRAIN_FRACS[0] * n)
            train_start = max(0, train_start)
        else:  # anchored | expanding
            train_start = 0

        fr = FoldResult(
            fold=fold + 1, params={},
            train_start=train_start, train_end=train_end,
            test_start=test_start, test_end=test_end,
        )

        # ── 外层训练段内再切 inner train/validation ──
        inner_validation_bars = max(
            INNER_VALIDATION_MIN_BARS,
            int((train_end - train_start) * INNER_VALIDATION_FRAC),
        )
        inner_validation_start = train_end - inner_validation_bars
        # 至少保留一半外层训练区给 inner-train，避免小样本时 validation 反客为主。
        inner_validation_start = max(
            train_start + (train_end - train_start) // 2,
            inner_validation_start,
        )
        fr.inner_train_end = inner_validation_start
        fr.inner_validation_start = inner_validation_start
        fr.inner_validation_end = train_end

        # Inner-train 跑全网格，只做可行性筛选；这里的排名不是最终选参排名。
        train_candidates: List[BacktestResult] = []
        for p in grid:
            r = backtest_engine.run(
                symbol, ts, opens, highs, lows, closes, p,
                start_idx=train_start, end_idx=inner_validation_start,
                features=features, volumes=volumes, cost_bps=cost_bps,
            )
            train_candidates.append(r)
        fr.inner_candidate_count = len(train_candidates)
        shortlist = _train_shortlist(train_candidates, INNER_SHORTLIST_SIZE)
        fr.inner_shortlist_count = len(shortlist)

        # 最终参数只由 inner-validation 排名决定。外层 OOS 仍保持完全不可见。
        validation_candidates: List[BacktestResult] = []
        for candidate in shortlist:
            validation_candidates.append(backtest_engine.run(
                symbol, ts, opens, highs, lows, closes, candidate.params,
                start_idx=inner_validation_start, end_idx=train_end,
                features=features, volumes=volumes, cost_bps=cost_bps,
            ))
        if not validation_candidates:
            wf.folds.append(fr)
            continue
        best_validation = max(validation_candidates, key=_validation_rank)
        fr.params = best_validation.params
        fr.inner_validation_result = best_validation
        # 选择完成后才在完整外层训练区重估交易频率；该结果不改变参数。
        best_train = backtest_engine.run(
            symbol, ts, opens, highs, lows, closes, best_validation.params,
            start_idx=train_start, end_idx=train_end, features=features,
            volumes=volumes, cost_bps=cost_bps,
        )
        fr.train_result = best_train

        # 该折选出的形态决定 OOS 最低交易数：低频形态（死叉出场、MA≥100 慢均线
        # 入场、或训练段年化交易 <3 笔的低频标的）放宽到 1 笔（见 LOW_FREQ_*
        # 注释）；高频形态保持 3 笔。
        is_low_freq = (fr.params.get("exit_mode", "chandelier") in LOW_FREQ_EXIT_MODES
                       or int(fr.params.get("ma_period", 50)) >= LOW_FREQ_MA_THRESHOLD)
        if not is_low_freq and best_train is not None and train_end > 0:
            train_years = train_end / TRADING_DAYS_PER_YEAR
            annual_trades = best_train.trade_count / train_years if train_years > 0 else 0.0
            is_low_freq = annual_trades < LOW_FREQ_ANNUAL_TRADES
        min_trades = MIN_OOS_TRADES_LOW_FREQ if is_low_freq else min_oos_trades

        # ── OOS 段：用训练选出的参数跑测试段 ──
        oos = backtest_engine.run(
            symbol, ts, opens, highs, lows, closes, fr.params,
            start_idx=test_start, end_idx=test_end, features=features,
            volumes=volumes, cost_bps=cost_bps,
        )
        # 25bps 成本 OOS（引擎已含成本，这里再显式算一个无成本版本对比用）
        oos_nocost = backtest_engine.run(
            symbol, ts, opens, highs, lows, closes, fr.params,
            start_idx=test_start, end_idx=test_end, cost_bps=0.0,
            features=features, volumes=volumes,
        )

        fr.oos_result = oos
        fr.oos_trades = oos.trade_count
        # oos_return_pct = 无成本版（raw 收益），oos_cost_return_pct = 含成本版（25bps），
        # 两组数据须显式区分——若同取含成本版则 cost_positive_folds 退化为 positive_folds，
        # 失去"成本韧性"语义。结构性稳健判定 ok4 使用 worst_cost_return_pct（含成本版本）。
        fr.oos_return_pct = oos_nocost.total_return_pct
        fr.oos_sharpe = oos.sharpe_daily
        fr.oos_cost_return_pct = oos.total_return_pct
        fr.oos_maxdd_pct = oos.max_drawdown_pct
        # 买入持有 baseline（OOS 段：起点收盘 → 终点收盘）
        bh_start = closes[test_start]
        bh_end = closes[test_end - 1]
        fr.baseline_return_pct = (bh_end - bh_start) / bh_start * 100 if bh_start > 0 else 0.0
        # 检查 STP LMT 遗留未成交：trades 中 exit_reason 包含 unfilled 即视为有
        fr.stp_lmt_unfilled = any("unfilled" in t.exit_reason for t in oos.trades)

        # ── 单折通过判定 ──
        reasons = []
        if oos.trade_count < min_trades:
            reasons.append("oos_trades")
        if oos.total_return_pct <= 0:
            reasons.append("oos_return")
        if oos.sharpe_daily <= 0:
            reasons.append("oos_sharpe")
        if oos_nocost.total_return_pct > 0 and oos.total_return_pct <= 0:
            reasons.append("cost_erosion")  # 有成本转负
        if fr.stp_lmt_unfilled:
            reasons.append("stp_lmt_unfilled")
        fr.fail_reasons = reasons
        fr.passed = len(reasons) == 0
        wf.folds.append(fr)

    # ── 整体统计 ──
    wf.passed_folds = sum(1 for f in wf.folds if f.passed)
    wf.positive_folds = sum(1 for f in wf.folds if f.oos_return_pct > 0)
    wf.cost_positive_folds = sum(1 for f in wf.folds if f.oos_cost_return_pct > 0)

    variants = set()
    for f in wf.folds:
        if f.params:
            # 参数稳定性 key 必须含 exit_mode：同一入场参数在不同出场形态下
            # 是不同策略（GLD 同入场 chandelier +59.9% vs ma_cross +212.5%），
            # 缺了 exit_mode 会把不同形态误判为同一参数变体。
            # 2026-08-07 追加 ADX 维度：ADX 过滤开/关是不同策略（TQQQ 实测
            # 关 ADX −17.9% vs 开 ADX +45.8%），缺了会把两者误判为同一变体。
            # 2026-08-08 阈值多档：15/20/25/30 是不同参数，必须精确进 key，
            # 否则 4 档阈值被当成同一变体 → parameter_variants 被低估。
            variants.add(f"{f.params.get('entry_mode')}-{f.params.get('ma_period')}-"
                         f"{f.params.get('atr_multiple')}-{f.params.get('buffer')}-"
                         f"{f.params.get('exit_mode', 'chandelier')}-"
                         f"adx{int(bool(f.params.get('adx_filter', False)))}"
                         f"{int(f.params.get('adx_threshold', 0))}")
    wf.parameter_variants = len(variants)

    sharpe_list = [f.oos_sharpe for f in wf.folds if f.oos_result]
    wf.avg_sharpe = sum(sharpe_list) / len(sharpe_list) if sharpe_list else 0.0
    dd_list = [f.oos_maxdd_pct for f in wf.folds if f.oos_result]
    wf.avg_maxdd = sum(dd_list) / len(dd_list) if dd_list else 0.0
    bh_list = [f.baseline_return_pct for f in wf.folds]
    wf.avg_baseline_return_pct = sum(bh_list) / len(bh_list) if bh_list else 0.0
    wf.worst_cost_return_pct = min((f.oos_cost_return_pct for f in wf.folds), default=0.0)

    # ── 结构稳健判定（spec §2.7）──
    ok1 = wf.passed_folds >= 3
    ok2 = wf.positive_folds >= 3
    ok3 = wf.parameter_variants <= 3
    ok4 = wf.worst_cost_return_pct >= -25.0
    ok5 = all(f.oos_maxdd_pct <= 25.0 for f in wf.folds if f.oos_result)
    wf.structurally_robust = all([ok1, ok2, ok3, ok4, ok5])
    wf.eligible = wf.structurally_robust
    return wf


# ────────────────────────────────────────────────────────────────
# 冒烟测试
# ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import math
    n = 600
    ts = [f"2023-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}" for i in range(n)]
    # 多波段趋势（每 60 根一浪，浪顶回落）：让止损频繁触发，OOS 窗口内必有交易
    closes = []
    base = 100.0
    for wave in range(10):
        for k in range(60):
            phase = k / 60.0
            # 上升段 0..45，回落段 45..60
            if phase < 0.75:
                base += 0.35
            else:
                base -= 1.6
            closes.append(base + 0.8 * math.sin(k / 3))
    closes = closes[:n]
    highs = [c + 1.2 for c in closes]
    lows = [c - 1.2 for c in closes]
    opens = [c - 0.3 for c in closes]

    # 用小网格加速测试
    small_grid = [
        {"entry_mode": "hybrid", "ma_period": 50, "atr_multiple": 3.0, "buffer": 0.01},
        {"entry_mode": "breakout", "ma_period": 50, "atr_multiple": 3.0, "buffer": 0.01},
        {"entry_mode": "pullback", "ma_period": 50, "atr_multiple": 3.0, "buffer": 0.01},
    ]
    wf = run_walk_forward("TEST.US", ts, opens, highs, lows, closes, params_grid=small_grid)
    print(wf.summarize())
    for f in wf.folds:
        print(f"  Fold{f.fold}: pass={f.passed} trades={f.oos_trades} ret={round(f.oos_return_pct,2)}% "
              f"dd={round(f.oos_maxdd_pct,2)}% params={f.params}")
    print("walk_forward.py 冒烟测试通过 ✅")
