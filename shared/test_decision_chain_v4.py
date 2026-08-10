import math

from research.walk_forward import run_walk_forward
from shared.alpha_model import RuleBasedAlpha
from shared.backtest import run_backtest
from shared.backtest_engine import (
    BacktestEngine, NativeBacktestEngine, VectorbtBacktestEngine,
)


def _market(n=320):
    closes = [100 + i * 0.15 + math.sin(i / 8) for i in range(n)]
    return (
        [str(i) for i in range(n)],
        [v - 0.2 for v in closes],
        [v + 1 for v in closes],
        [v - 1 for v in closes],
        closes,
    )


def test_native_engine_preserves_direct_backtest_semantics():
    ts, opens, highs, lows, closes = _market()
    params = {"entry_mode": "momentum", "ma_period": 20,
              "atr_multiple": 3.0, "buffer": 0.01,
              "exit_mode": "chandelier"}
    direct = run_backtest("TEST.US", ts, opens, highs, lows, closes, params)
    engine = NativeBacktestEngine()
    features = engine.prepare(closes, highs, lows)
    adapted = engine.run("TEST.US", ts, opens, highs, lows, closes, params,
                         features=features)
    assert isinstance(engine, BacktestEngine)
    assert direct.stats() == adapted.stats()
    assert [t.__dict__ for t in direct.trades] == [t.__dict__ for t in adapted.trades]


def test_alpha_signal_contains_feature_lab_snapshot():
    ts, opens, highs, lows, closes = _market()
    engine = NativeBacktestEngine()
    features = engine.prepare(closes, highs, lows)
    from shared.indicators import ma_slope, sma
    mas = sma(closes, 20)
    slopes = ma_slope(closes, 20, 20)
    signals = RuleBasedAlpha("momentum", 0.01, 20).generate(
        "TEST.US", closes, mas, slopes, features=features,
    )
    assert signals
    assert "momentum_20" in signals[0].feature_snapshot
    assert "rsi_14" in signals[0].feature_snapshot


def test_walk_forward_accepts_engine_boundary():
    ts, opens, highs, lows, closes = _market(400)
    grid = [{"entry_mode": "momentum", "ma_period": 20,
             "atr_multiple": 3.0, "buffer": 0.01,
             "exit_mode": "chandelier"}]
    result = run_walk_forward(
        "TEST.US", ts, opens, highs, lows, closes,
        params_grid=grid, engine=NativeBacktestEngine(),
    )
    assert len(result.folds) == 4


def test_vectorbt_adapter_is_scan_only_and_maps_costs():
    calls = []

    class Portfolio:
        @staticmethod
        def from_signals(closes, **kwargs):
            calls.append((closes, kwargs))
            return "portfolio"

    fake_vbt = type("FakeVbt", (), {"Portfolio": Portfolio})
    engine = VectorbtBacktestEngine(fake_vbt)
    assert engine.scan([1, 2], [True, False], [False, True], cost_bps=25) == "portfolio"
    assert calls[0][1]["fees"] == 0.0025
    import pytest
    with pytest.raises(RuntimeError, match="Native"):
        engine.run()
