from types import SimpleNamespace

from application.contracts import TradingCatApplication
from production.portfolio_risk import PositionPlan, check_portfolio
from research.factors import analyze_factor_snapshot
from research.pipeline import _evaluate_final_holdout
from research.walk_forward import run_walk_forward, trade_statistics
from shared import db as dbm
from shared.backtest import BacktestResult, Trade
from shared.cost_model import estimate_cost


class DeterministicEngine:
    """A inner-train 更好，B inner-validation 更好，用来证明最终按 validation 选。"""

    def prepare(self, closes, highs, lows, volumes=None):
        return [{} for _ in closes]

    def run(self, symbol, ts, opens, highs, lows, closes, params, **kwargs):
        start, end = kwargs["start_idx"], kwargs["end_idx"]
        is_inner_validation = start > 0 and end in (200, 250, 300, 350)
        preferred = params["name"] == ("B" if is_inner_validation else "A")
        result = BacktestResult(symbol, params)
        result.trade_count = 10
        result.kelly = 0.4 if preferred else 0.1
        result.total_return_pct = 8.0 if preferred else 1.0
        result.sharpe_daily = 1.0 if preferred else 0.2
        return result


def _market(n=400):
    closes = [100.0 + i * 0.1 for i in range(n)]
    return ([str(i) for i in range(n)], closes, closes, closes, closes)


def test_nested_walk_forward_selects_on_inner_validation_not_inner_train():
    ts, opens, highs, lows, closes = _market()
    grid = [{"name": "A", "entry_mode": "momentum", "ma_period": 20,
             "atr_multiple": 3.0, "buffer": 0.0, "exit_mode": "chandelier"},
            {"name": "B", "entry_mode": "momentum", "ma_period": 20,
             "atr_multiple": 3.0, "buffer": 0.0, "exit_mode": "chandelier"}]
    result = run_walk_forward(
        "T.US", ts, opens, highs, lows, closes, params_grid=grid,
        engine=DeterministicEngine())
    assert result.folds[0].params["name"] == "B"
    assert result.folds[0].inner_validation_start == 158
    assert result.folds[0].inner_candidate_count == 2


def test_frozen_candidate_statistics_do_not_mix_other_parameter_trades():
    trades = [Trade(1, 100, 2, 110, pnl_pct=10),
              Trade(3, 100, 4, 95, pnl_pct=-5)]
    stats = trade_statistics(trades, positive_periods=1, total_periods=2)
    assert stats == {"n": 2, "wins": 1, "losses": 1, "avg_win": 10.0,
                     "avg_loss": 5.0, "p": 0.5, "b": 2.0,
                     "positive_folds": 1, "total_folds": 2}


def test_final_holdout_requires_more_than_one_lucky_trade(monkeypatch):
    result = BacktestResult("T.US", {})
    result.trade_count = 2
    result.total_return_pct = 5.0
    result.sharpe_daily = 1.0
    monkeypatch.setattr("research.pipeline.run_backtest", lambda *a, **k: result)
    measured = _evaluate_final_holdout(
        "T.US", [str(i) for i in range(130)], [1.0] * 130, [1.0] * 130,
        [1.0] * 130, [1.0] * 130, {}, 4)
    assert measured["passed"] is False
    assert measured["reasons"] == ["holdout_insufficient_trades"]


def test_factor_snapshot_is_point_in_time_and_does_not_backfill_future_report():
    conn = dbm.get_conn(":memory:")
    rows = [{"ts": f"2025-{i // 28 + 1:02d}-{i % 28 + 1:02d}",
             "open": 100 + i, "high": 101 + i, "low": 99 + i,
             "close": 100 + i, "volume": 1_000_000} for i in range(130)]
    dbm.upsert_bars(conn, "T.US", rows, "test")
    dbm.upsert_fundamental(conn, "T.US", "2025-03-31", "2025-05-01",
                           "2025-05-02", {"revenue": 100}, source="test")
    before = analyze_factor_snapshot(conn, "T.US", "2025-05-01")
    after = analyze_factor_snapshot(conn, "T.US", "2025-05-02")
    assert before["fundamental"] is None
    assert after["fundamental"][0]["values"]["revenue"] == 100


def test_cost_model_increases_for_illiquid_order():
    liquid = estimate_cost("T.US", [100] * 60, [10_000_000] * 60, 10_000)
    illiquid = estimate_cost("T.US", [10] * 60, [1_000] * 60, 10_000)
    assert illiquid.total_bps_per_side > liquid.total_bps_per_side


def test_portfolio_factor_risk_rejects_sector_concentration():
    conn = dbm.get_conn(":memory:")
    plans = [PositionPlan("A.US", 0.09, None, None, sector="TECH", currency="USD"),
             PositionPlan("B.US", 0.08, None, None, sector="TECH", currency="USD")]
    result = check_portfolio(conn, 100_000, plans)
    assert not result.passed
    assert any(reason.startswith("sector_TECH") for reason in result.failures)


def test_review_portfolio_can_recommend_add_to_kelly_target(monkeypatch):
    conn = dbm.get_conn(":memory:")
    app = TradingCatApplication(conn)
    params = {"entry_mode": "momentum", "ma_period": 20,
              "atr_multiple": 3.0, "buffer": 0.0,
              "exit_mode": "chandelier"}
    import json
    dbm.set_lifecycle(conn, "AAPL.US", "verified", params_json=json.dumps(params))
    dbm.save_strategy_version(
        conn, "AAPL.US", "verified", params_json=json.dumps(params),
        wf_report_json="{}", git_commit=None, code_hash="code", data_version="data",
        oos_stats_json=json.dumps({
            "n": 20, "wins": 14, "losses": 6, "p": .7, "b": 2,
            "avg_win": 2, "avg_loss": 1, "positive_folds": 2,
            "total_folds": 2, "final_test_accepted": True}),
    )
    rows = [{"ts": f"2025-{i // 28 + 1:02d}-{i % 28 + 1:02d}",
             "open": 100, "high": 101, "low": 99, "close": 100,
             "volume": 10_000_000} for i in range(130)]
    dbm.upsert_bars(conn, "AAPL.US", rows, "test")
    monkeypatch.setattr("application.contracts.post_market_check",
                        lambda *a, **k: SimpleNamespace(exit_triggered=False, messages=[]))
    account = SimpleNamespace(
        nav=100_000, synced=True,
        positions=[{"symbol": "AAPL.US", "quantity": 10, "last_price": 100,
                    "cost_price": 100, "stop_price": 95}])
    item = app.review_portfolio(account_state=account)["data"]["position_advice"][0]
    assert item["action"] == "ADD"
    assert item["target_weight"] > item["current_weight"]
