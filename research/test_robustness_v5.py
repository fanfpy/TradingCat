from types import SimpleNamespace

from production.position import KellyPositionSizer
from research.robustness import (
    fold_parameter_stability,
    multiple_testing_diagnostic,
    parameter_neighborhood_diagnostic,
)


def test_search_penalty_grows_with_trials_and_rejects_noise_like_sharpe():
    small = multiple_testing_diagnostic(0.10, trials=2, observations=500)
    large = multiple_testing_diagnostic(0.10, trials=5000, observations=500)
    assert large["penalty"] > small["penalty"]
    assert not large["passed"]
    assert multiple_testing_diagnostic(1.0, 100, 500)["passed"]


def test_fold_parameter_stability_rejects_four_unrelated_optima():
    stable = fold_parameter_stability([{"ma": 20}, {"ma": 20},
                                       {"ma": 50}, {"ma": 20}])
    unstable = fold_parameter_stability([{"ma": 10}, {"ma": 20},
                                         {"ma": 50}, {"ma": 100}])
    assert stable["passed"]
    assert not unstable["passed"]


def test_parameter_neighborhood_rejects_isolated_peak(monkeypatch):
    def fake_run(_symbol, _ts, _opens, _highs, _lows, _closes, params, **_kwargs):
        value = 10.0 if params["ma"] == 20 else -5.0
        return SimpleNamespace(total_return_pct=value)

    monkeypatch.setattr("research.robustness.run_backtest", fake_run)
    result = parameter_neighborhood_diagnostic(
        "T", ["1", "2"], [1, 1], [1, 1], [1, 1], [1, 1],
        {"ma": 20, "atr": 3},
        [{"ma": 20, "atr": 3}, {"ma": 10, "atr": 3},
         {"ma": 50, "atr": 3}, {"ma": 20, "atr": 2}],
    )
    assert not result["passed"]
    assert result["positive_share"] < 0.6


def test_kelly_cannot_consume_statistics_without_final_acceptance():
    signal = {
        "symbol": "T.US", "entry_price": 100.0, "stop_price": 90.0,
        "oos_stats": {"n": 50, "p": 0.8, "b": 3.0,
                      "positive_folds": 4, "total_folds": 4},
    }
    intent = KellyPositionSizer().size(signal, 100_000)[0]
    assert intent.target_fraction == 0.0
    assert intent.evidence["executable"] is False
