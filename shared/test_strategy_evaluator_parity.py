"""P0-A：回测、重放与生产监控必须共享同一策略语义。"""

import math
from datetime import date, timedelta

import pytest

from production.monitor import post_market_check
from shared import db as dbm
from shared.backtest import run_backtest
from shared.strategy_evaluator import ENTRY_MODES, StrategyEvaluator


def _series(n=320):
    closes = [100 + 0.04 * i + 6 * math.sin(i / 9) for i in range(n)]
    return closes[:], [c + 1 for c in closes], [c - 1 for c in closes], closes


def _params(entry_mode, *, adx=False, exit_mode="chandelier_or_cross"):
    return {
        "entry_mode": entry_mode,
        "ma_period": 50,
        "atr_multiple": 3.0,
        "buffer": 0.03,
        "exit_mode": exit_mode,
        "adx_filter": adx,
        "adx_threshold": 15,
        "adx_direction": adx,
    }


@pytest.mark.parametrize("entry_mode", ENTRY_MODES)
@pytest.mark.parametrize("adx", (False, True))
def test_backtest_first_entry_matches_strategy_evaluator(entry_mode, adx):
    opens, highs, lows, closes = _series()
    params = _params(entry_mode, adx=adx)
    evaluator = StrategyEvaluator(opens, highs, lows, closes, params)
    expected = next(
        i for i in range(len(closes)) if evaluator.evaluate_entry(i).triggered)
    ts = [f"D{i:04d}" for i in range(len(closes))]
    result = run_backtest(
        "PARITY.US", ts, opens, highs, lows, closes, params, stp_lmt=False)
    assert result.trades
    assert result.trades[0].entry_idx == expected


def _store_bars(conn, symbol, opens, highs, lows, closes):
    start = date.today() - timedelta(days=len(closes) - 1)
    rows = [{
        "ts": (start + timedelta(days=i)).isoformat(),
        "open": opens[i], "high": highs[i], "low": lows[i],
        "close": closes[i], "volume": 1_000_000,
    } for i in range(len(closes))]
    dbm.upsert_bars(conn, symbol, rows, "parity")
    return rows


def test_production_golden_cross_entry_matches_evaluator():
    opens, highs, lows, closes = _series()
    params = _params("golden_cross")
    full = StrategyEvaluator(opens, highs, lows, closes, params)
    trigger = next(i for i in range(50, len(closes))
                   if full.evaluate_entry(i).triggered)
    opens, highs, lows, closes = (
        values[:trigger + 1] for values in (opens, highs, lows, closes))
    evaluator = StrategyEvaluator(opens, highs, lows, closes, params)
    assert evaluator.evaluate_entry(trigger).triggered

    conn = dbm.get_conn(":memory:")
    _store_bars(conn, "GC.US", opens, highs, lows, closes)
    report = post_market_check(conn, "GC.US", params, date.today().isoformat())
    assert report.formal_entry


def test_production_ma_cross_exit_matches_evaluator():
    opens, highs, lows, closes = _series()
    params = _params("momentum", exit_mode="ma_cross")
    full = StrategyEvaluator(opens, highs, lows, closes, params)
    trigger = next(i for i in range(100, len(closes))
                   if full.evaluate_exit(i, max(highs[:i + 1])).triggered)
    opens, highs, lows, closes = (
        values[:trigger + 1] for values in (opens, highs, lows, closes))
    peak = max(highs)
    evaluator = StrategyEvaluator(opens, highs, lows, closes, params)
    expected = evaluator.evaluate_exit(trigger, peak)
    assert expected.triggered and expected.reason == "ma_cross"

    conn = dbm.get_conn(":memory:")
    rows = _store_bars(conn, "DC.US", opens, highs, lows, closes)
    dbm.upsert_position(
        conn, "DC.US", closes[100], rows[100]["ts"], 10, 1.0, peak)
    report = post_market_check(conn, "DC.US", params, date.today().isoformat())
    assert report.exit_triggered
    assert any("ma_cross" in message for message in report.messages)

