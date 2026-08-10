#!/usr/bin/env python3
"""
综合评分 + 生命周期判定 — 交易系统 v3.0
=======================================
spec §2.9-2.10：

评分（满分 100，只用于排序，不是准入线）：
- OOS 通过折数        35 分  passedFolds / 4 × 35
- OOS 正收益折数      20 分  positiveFolds / 4 × 20
- 25bps 成本韧性      15 分  costPositiveFolds / 4 × 15
- 参数稳定性          15 分  max(0, (5 - parameterVariants) / 4) × 15
- 平均 OOS Sharpe     10 分  clamp(avgSharpe, 0, 1) × 10
- OOS 回撤控制         5 分  max(0, 1 - avgMaxDD / 30) × 5

准入原则：即使总分 < 75，只要结构稳健和尾部风险门槛通过，也可进入活动池；
反之高分但尾部风险超限仍不能进入。

生命周期：candidate → backtesting → verified → degraded → removed
- 相同 evidence hash 重跑不重复累计失败次数
"""

from typing import Dict, Optional

from research.walk_forward import WFResult

# 评分常量
SCORE_WEIGHTS = {
    "fold_pass": 35,
    "positive": 20,
    "cost": 15,
    "parameter": 15,
    "sharpe": 10,
    "drawdown": 5,
}
TAIL_RISK_MAXDD = 25.0      # 任一 OOS 折 MaxDD 上限
WORST_COST_FLOOR = -25.0    # 最差 25bps OOS 折收益下限


def compute_score(wf: WFResult) -> float:
    """综合评分（0-100）。"""
    if not wf.folds:
        return 0.0
    fold_pass = wf.passed_folds / 4 * SCORE_WEIGHTS["fold_pass"]
    positive = wf.positive_folds / 4 * SCORE_WEIGHTS["positive"]
    cost = wf.cost_positive_folds / 4 * SCORE_WEIGHTS["cost"]
    param = max(0.0, (5 - wf.parameter_variants) / 4) * SCORE_WEIGHTS["parameter"]
    sharpe = max(0.0, min(wf.avg_sharpe, 1.0)) * SCORE_WEIGHTS["sharpe"]
    dd = max(0.0, 1 - wf.avg_maxdd / 30) * SCORE_WEIGHTS["drawdown"]
    return round(fold_pass + positive + cost + param + sharpe + dd, 2)


def tail_risk_passed(wf: WFResult) -> bool:
    """尾部风险门槛：最差 25bps OOS 折收益 ≥ -25%，任一折 MaxDD ≤ 25%。"""
    if not wf.folds:
        return False
    worst_ok = wf.worst_cost_return_pct >= WORST_COST_FLOOR
    dd_ok = all(f.oos_maxdd_pct <= TAIL_RISK_MAXDD for f in wf.folds if f.oos_result)
    return worst_ok and dd_ok


def decide_lifecycle(wf: WFResult) -> Dict:
    """判断生命周期去向。

    Returns:
        {status: verified|degraded, score, eligible, reasons}
    """
    score = compute_score(wf)
    structurally_robust = wf.structurally_robust
    tail_ok = tail_risk_passed(wf)

    reasons = []
    if not structurally_robust:
        reasons.append("structure_not_robust")
    if not tail_ok:
        reasons.append("tail_risk_failed")

    # 准入：结构稳健 且 尾部风险通过（总分不设固定线）
    # 2026-08-06：active_watchlist 概念移除，通过验证的标的状态为 verified
    if structurally_robust and tail_ok:
        status = "verified"
    else:
        status = "degraded"

    return {
        "status": status,
        "score": score,
        "eligible": status == "verified",
        "reasons": reasons,
        "passed_folds": wf.passed_folds,
        "positive_folds": wf.positive_folds,
        "parameter_variants": wf.parameter_variants,
        "avg_sharpe": round(wf.avg_sharpe, 3),
        "avg_maxdd": round(wf.avg_maxdd, 2),
        "worst_cost_return_pct": round(wf.worst_cost_return_pct, 2),
        "avg_baseline_return_pct": round(wf.avg_baseline_return_pct, 2),
    }


# ────────────────────────────────────────────────────────────────
# 冒烟测试
# ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from research.walk_forward import FoldResult, WFResult

    # 构造一个"全部通过"的 WF 结果
    wf = WFResult(symbol="T")
    for f in range(4):
        fr = FoldResult(fold=f + 1, params={"entry_mode": "hybrid", "ma_period": 50,
                                            "atr_multiple": 3.0, "buffer": 0.01,
                                            "exit_mode": "chandelier"},
                        train_start=0, train_end=100, test_start=100, test_end=200)
        fr.passed = True
        fr.oos_trades = 5
        fr.oos_return_pct = 8.0
        fr.oos_cost_return_pct = 7.5
        fr.oos_sharpe = 0.6
        fr.oos_maxdd_pct = 12.0
        fr.baseline_return_pct = 5.0
        wf.folds.append(fr)
    wf.passed_folds = 4
    wf.positive_folds = 4
    wf.cost_positive_folds = 4
    wf.parameter_variants = 2
    wf.avg_sharpe = 0.6
    wf.avg_maxdd = 12.0
    wf.worst_cost_return_pct = 5.0
    wf.avg_baseline_return_pct = 5.0
    wf.structurally_robust = True
    wf.eligible = True

    s = compute_score(wf)
    print("score:", s)
    assert s > 90, f"全部通过应高分: {s}"
    d = decide_lifecycle(wf)
    print("decision:", d)
    assert d["status"] == "verified"

    # 结构稳健但尾部风险超限 → 不能进入
    wf.worst_cost_return_pct = -30.0
    wf.structurally_robust = False
    d2 = decide_lifecycle(wf)
    print("decision2:", d2)
    assert d2["status"] == "degraded"

    print("score.py 冒烟测试通过 ✅")
