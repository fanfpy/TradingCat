from datetime import date, timedelta

import pytest

from production.position import PositionIntent
from production.target_portfolio import _risk_metadata, build_target_portfolio
from research.pipeline import _small_grid, add_candidate, cache_symbol, research_candidate
from shared import db as dbm
from shared.longbridge_client import LongbridgeClient
from shared.security import (
    SecurityService, UnknownSecurityMetadataError,
)


class FakeLongbridgeClient(LongbridgeClient):
    def __init__(self, response):
        self.response = response
        self.static_calls = []
        self._quote_ctx = self

    def static_info(self, symbols):
        if isinstance(symbols, list):
            self.static_calls.extend(symbols)
            return [self.response]
        return super().static_info(symbols)

    def kline_by_count(self, symbol, count, period):
        start = date.today() - timedelta(days=count - 1)
        rows = []
        for index in range(count):
            close = 100 + index * 0.2 + (index % 7)
            rows.append({
                "timestamp": str(start + timedelta(days=index)),
                "open": close - 0.2, "high": close + 1,
                "low": close - 1, "close": close,
                "volume": 20_000_000, "turnover": close * 20_000_000,
            })
        return rows

    def trading_calendar(self, market, start, end):
        return [{"trade_date": str(start + timedelta(days=index)), "is_open": True}
                for index in range((end - start).days + 1)]


def _static_response(symbol, *, board="SecurityBoard.USMain"):
    return type("StaticInfo", (), {
        "symbol": symbol, "name_en": "Advanced Micro Devices",
        "name_cn": "", "name_hk": "", "exchange": "NASD",
        "currency": "USD", "lot_size": 1, "board": board,
        "total_shares": 1_600_000_000,
        "circulating_shares": 1_500_000_000,
        "eps": 2.6, "eps_ttm": 3.9, "bps": 41.0,
        "dividend_yield": 0, "hk_shares": 0,
    })()


def test_metadata_golden_path_hydrates_once_then_reaches_risk_metadata():
    conn = dbm.get_core_conn(":memory:")
    client = FakeLongbridgeClient(_static_response("AMD.US"))
    service = SecurityService(conn, client)

    first = service.ensure_metadata("AMD.US")
    second = service.ensure_metadata("AMD.US")
    add_candidate(conn, "AMD.US", security_service=service)
    cached = cache_symbol(conn, "AMD.US", count=630, client=client)
    researched = research_candidate(conn, "AMD.US", grid=_small_grid())
    portfolio = build_target_portfolio(conn, 100_000, [PositionIntent(
        "AMD.US", "BUY", 0.01, entry_price=200, stop_price=180)])

    assert first["asset_type"] == "EQUITY"
    assert second["metadata_source"] == "security_master"
    assert client.static_calls == ["AMD.US"]
    assert cached["bar_count"] == 630
    assert researched.get("error_type") is None
    assert portfolio.intents[0].symbol == "AMD.US"
    assert _risk_metadata(conn, "AMD.US")["asset_type"] == "EQUITY"


def test_unknown_asset_type_is_rejected_by_risk_chain():
    conn = dbm.get_core_conn(":memory:")
    client = FakeLongbridgeClient(
        _static_response("MYSTERY.US", board="SecurityBoard.Unknown"))
    result = SecurityService(conn, client).ensure_batch(["MYSTERY.US"])[0]
    assert result["ok"] is False

    with pytest.raises(UnknownSecurityMetadataError, match="UNKNOWN_METADATA"):
        build_target_portfolio(conn, 100_000, [PositionIntent(
            "MYSTERY.US", "BUY", 0.01, entry_price=10, stop_price=9)])