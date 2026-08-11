from datetime import date, timedelta

from production.operations import check_current_portfolio, sync_runtime_state
from research.pipeline import add_candidate, cache_symbol, sync_watchlist
from shared import db as dbm
from shared.security import SecurityService


def _metadata(symbol, currency="USD"):
    return {"symbol": symbol, "name": symbol, "exchange": "NASDAQ",
            "currency": currency, "asset_type": "EQUITY", "lot_size": 1}


class MetadataProvider:
    def __init__(self, metadata):
        self.metadata = metadata
        self.calls = []

    def static_info(self, symbol):
        self.calls.append(symbol)
        return self.metadata[symbol]


class WatchlistClient(MetadataProvider):
    def watchlist(self, strict=True):
        return list(self.metadata)


class ResearchClient(MetadataProvider):
    def kline_by_count(self, symbol, count, period):
        start = date.today() - timedelta(days=count - 1)
        return [{"timestamp": str(start + timedelta(days=index)),
                 "open": 100, "high": 101, "low": 99, "close": 100,
                 "volume": 20_000_000, "turnover": 2_000_000_000}
                for index in range(count)]

    def trading_calendar(self, market, start, end):
        return [{"trade_date": str(start + timedelta(days=index)), "is_open": True}
                for index in range((end - start).days + 1)]


class AccountClient:
    def assets(self):
        return {"total_cash": 100_000, "max_finance_amount": 50_000,
                "net_assets": 120_000}

    def positions(self, strict=True):
        return [{"symbol": "0700.HK", "quantity": "10", "cost_price": "400"}]

    def orders(self, strict=True):
        return []


def test_watchlist_sync_hydrates_new_candidates():
    conn = dbm.get_core_conn(":memory:")
    client = WatchlistClient({"AAPL.US": _metadata("AAPL.US")})

    result = sync_watchlist(conn, client=client)

    assert result["added_candidate"] == ["AAPL.US"]
    assert result["metadata"][0]["ok"] is True
    assert client.calls == ["AAPL.US"]
    assert dbm.get_security(conn, "AAPL.US")["asset_type"] == "EQUITY"


def test_research_add_then_cache_calls_metadata_provider_once():
    conn = dbm.get_core_conn(":memory:")
    client = ResearchClient({"AMD.US": _metadata("AMD.US")})
    service = SecurityService(conn, client)

    add_candidate(conn, "AMD.US", security_service=service)
    result = cache_symbol(conn, "AMD.US", count=630, client=client)

    assert result["bar_count"] == 630
    assert result["metadata"]["ok"] is True
    assert client.calls == ["AMD.US"]


def test_account_sync_hydrates_new_position_metadata_once():
    conn = dbm.get_core_conn(":memory:")
    dbm.upsert_position(conn, "0700.HK", 400, "2026-01-01", 10, 350, 470)
    dbm.upsert_bars(conn, "0700.HK", [{
        "ts": "2026-08-10", "open": 470, "high": 480, "low": 460,
        "close": 470, "volume": 20_000_000,
    }], "test")
    provider = MetadataProvider({"0700.HK": _metadata("0700.HK", "HKD")})
    service = SecurityService(conn, provider)

    result = sync_runtime_state(
        conn, client=AccountClient(), security_service=service)

    assert result["ok"] is True
    assert result["position_sync"]["metadata_failures"] == []
    assert provider.calls == ["0700.HK"]
    assert dbm.get_security(conn, "0700.HK")["currency"] == "HKD"
    risk = check_current_portfolio(conn)
    assert not any(item.startswith("unknown_security_metadata:")
                   for item in risk["failures"])