"""P0-A：生产参数不得读取 OOS 排名，最终 Holdout 只能暴露一次。"""

from research.pipeline import _production_params_from_training
from research.walk_forward import FoldResult, WFResult
from shared import db as dbm


def _fold(number, ma, oos_return):
    fold = FoldResult(
        fold=number,
        params={"entry_mode": "momentum", "ma_period": ma,
                "atr_multiple": 3.0, "buffer": 0.01,
                "exit_mode": "chandelier"},
        train_start=0, train_end=100, test_start=100, test_end=120,
    )
    fold.oos_cost_return_pct = oos_return
    return fold


def test_production_params_are_latest_training_choice_not_best_oos():
    wf = WFResult("TEST.US", folds=[
        _fold(1, 10, 999.0),
        _fold(2, 20, 5.0),
        _fold(3, 50, -50.0),
        _fold(4, 100, -999.0),
    ])
    selected = _production_params_from_training(wf)
    assert selected["ma_period"] == 100

    # 改变所有 OOS 排名不得改变生产参数。
    for index, fold in enumerate(wf.folds):
        fold.oos_cost_return_pct = 10_000.0 - index
    assert _production_params_from_training(wf) == selected


def test_holdout_is_consumed_once_and_exact_replay_uses_cache():
    conn = dbm.get_conn(":memory:")
    row = dbm.seal_research_holdout(
        conn, "ho_1", "A.US", "data-v1", "2025-01-01", "2025-06-01", "candidate-a")
    assert row["status"] == "SEALED"
    opened = dbm.open_research_holdout(conn, "ho_1", "candidate-a")
    assert opened["outcome"] == "OPENED"
    dbm.consume_research_holdout(
        conn, "ho_1", "candidate-a", {"passed": True, "return_pct": 3.5})

    replay = dbm.open_research_holdout(conn, "ho_1", "candidate-a")
    assert replay["outcome"] == "CACHED"
    assert replay["result"]["passed"] is True
    assert replay["row"]["exposure_count"] == 1


def test_changed_candidate_contaminates_consumed_holdout():
    conn = dbm.get_conn(":memory:")
    dbm.seal_research_holdout(
        conn, "ho_2", "A.US", "data-v1", "2025-01-01", "2025-06-01", "candidate-a")
    dbm.open_research_holdout(conn, "ho_2", "candidate-a")
    dbm.consume_research_holdout(conn, "ho_2", "candidate-a", {"passed": False})

    row = dbm.seal_research_holdout(
        conn, "ho_2", "A.US", "data-v1", "2025-01-01", "2025-06-01", "candidate-b")
    assert row["status"] == "CONTAMINATED"
    assert dbm.open_research_holdout(
        conn, "ho_2", "candidate-b")["outcome"] == "CONTAMINATED"


def test_crashed_open_holdout_fails_closed_on_reopen():
    conn = dbm.get_conn(":memory:")
    dbm.seal_research_holdout(
        conn, "ho_3", "A.US", "data-v2", "2025-07-01", "2025-12-01", "candidate-a")
    assert dbm.open_research_holdout(
        conn, "ho_3", "candidate-a")["outcome"] == "OPENED"
    reopened = dbm.open_research_holdout(conn, "ho_3", "candidate-a")
    assert reopened["outcome"] == "CONTAMINATED"
    assert reopened["row"]["status"] == "CONTAMINATED"

