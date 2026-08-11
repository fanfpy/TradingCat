from datetime import date, timedelta

from shared import db as dbm
from research.pipeline import cache_bars, prefilter, research_candidate


def _bars():
    return [{"ts": f"2026-07-{day:02d}", "open": 100, "high": 101,
             "low": 99, "close": 100, "volume": 200_000}
            for day in range(1, 21)]


def test_real_provider_manifest_fails_closed_without_calendar():
    conn = dbm.get_conn(":memory:")
    bars = _bars()
    cache_bars(conn, "AAPL.US", bars, "longbridge", "hash", bars[-1]["ts"])
    result = prefilter(conn, "AAPL.US", bars)
    assert "stale_completed_bar" in result["reasons"]
    assert "交易日历未覆盖" in result["metrics"]["freshness"]


def test_research_candidate_stops_when_prefilter_fails():
    conn = dbm.get_core_conn(":memory:")
    start = date(2022, 1, 1)
    bars = [{"ts": str(start + timedelta(days=index)), "open": 100,
             "high": 101, "low": 99, "close": 100, "volume": 200_000}
            for index in range(630)]
    cache_bars(conn, "AAPL.US", bars, "longbridge", "hash", bars[-1]["ts"])

    result = research_candidate(conn, "AAPL.US", grid=[])

    assert result["error_type"] == "PrefilterFailed"
    assert "stale_completed_bar" in result["prefilter"]["reasons"]
    assert dbm.get_lifecycle(conn, "AAPL.US") is None
