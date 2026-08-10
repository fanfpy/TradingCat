"""量化研究稳健性门槛：搜索惩罚、参数一致性与邻域测试。"""

import math
from statistics import median
from typing import Dict, Iterable, List

from shared.backtest import run_backtest


MIN_VERIFIED_OOS_TRADES = 12


def multiple_testing_diagnostic(avg_sharpe: float, trials: int,
                                observations: int) -> Dict:
    """保守的 Deflated-Sharpe 等效惩罚。

    网格越大、独立观测越少，要求的 Sharpe 越高。这里不声称是完整 Bailey/Lopez
    de Prado DSR（现有引擎未保存所有候选收益序列），但它是显式、单调且可审计
    的多重搜索惩罚，避免用未惩罚的最优 Sharpe 直接晋级。
    """
    effective_trials = max(1, int(trials))
    n = max(2, int(observations))
    penalty = math.sqrt(2.0 * math.log(effective_trials) / n)
    deflated = float(avg_sharpe) - penalty
    return {
        "method": "conservative_deflated_sharpe_equivalent",
        "trials": effective_trials,
        "observations": n,
        "penalty": round(penalty, 6),
        "raw_sharpe": round(float(avg_sharpe), 6),
        "deflated_sharpe": round(deflated, 6),
        "passed": deflated > 0.0,
    }


def fold_parameter_stability(fold_params: Iterable[Dict]) -> Dict:
    """度量各 WF 折训练阶段独立选出的参数是否集中。"""
    canonical = [tuple(sorted(p.items())) for p in fold_params if p]
    if not canonical:
        return {"passed": False, "reason": "no_fold_params", "mode_share": 0.0,
                "variants": 0}
    counts = {item: canonical.count(item) for item in set(canonical)}
    mode_share = max(counts.values()) / len(canonical)
    variants = len(counts)
    # 四折下允许 3 个变体，但至少有两折选择完全相同的参数。
    passed = variants <= 3 and (len(canonical) < 2 or mode_share >= 0.5)
    return {"passed": passed, "mode_share": round(mode_share, 4),
            "variants": variants, "folds": len(canonical)}


def _distance(candidate: Dict, other: Dict) -> int:
    return sum(candidate.get(key) != other.get(key)
               for key in set(candidate) | set(other))


def parameter_neighborhood_diagnostic(symbol: str, ts: List[str], opens: List[float],
                                      highs: List[float], lows: List[float],
                                      closes: List[float], candidate: Dict,
                                      search_grid: List[Dict],
                                      max_neighbors: int = 12) -> Dict:
    """在开发区重放候选参数的单维相邻点，拒绝孤立的尖峰最优值。"""
    neighbors = [p for p in search_grid if p != candidate and _distance(candidate, p) == 1]
    # 稳定排序使 manifest/测试可重复；最多取 12 个，控制全网格额外开销。
    neighbors = sorted(neighbors, key=lambda p: tuple(sorted(p.items())))[:max_neighbors]
    if not neighbors:
        return {"passed": True, "reason": "no_neighbors_in_search_space",
                "neighbors": 0, "positive_share": 1.0}

    base = run_backtest(symbol, ts, opens, highs, lows, closes, candidate,
                        start_idx=0, end_idx=len(closes))
    results = [run_backtest(symbol, ts, opens, highs, lows, closes, params,
                            start_idx=0, end_idx=len(closes))
               for params in neighbors]
    returns = [r.total_return_pct for r in results]
    positive_share = sum(value > 0 for value in returns) / len(returns)
    med = median(returns)
    # 邻域多数应保持正收益；若候选本身为正，邻域中位数不能崩成明显负值。
    passed = positive_share >= 0.6 and (base.total_return_pct <= 0 or med >= 0)
    return {
        "passed": passed,
        "neighbors": len(neighbors),
        "positive_share": round(positive_share, 4),
        "candidate_return_pct": round(base.total_return_pct, 6),
        "neighbor_median_return_pct": round(med, 6),
        "neighbor_min_return_pct": round(min(returns), 6),
        "neighbor_max_return_pct": round(max(returns), 6),
    }
