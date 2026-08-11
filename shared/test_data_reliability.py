from datetime import datetime, timedelta, timezone

import pytest

from production.monitor import health_check
from research.pipeline import HydrationRequiredError, cache_symbol
from shared import db as dbm
from shared.account import ensure_synced, load, sync_account, sync_positions
from shared.longbridge_client import NetworkError
from shared.security import SecurityService


def _metadata(symbol, **extra):
    return {"symbol": symbol, "name": symbol, "exchange": "NASDAQ",
            "currency": "USD", "asset_type": "EQUITY", "lot_size": 1,
            **extra}


class _Provider:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def static_info(self, symbol):
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class _AccountClient:
    def __init__(self, metadata=None):
        self.metadata = metadata
        self.kline_calls = 0

    def assets(self):
        return {"total_cash": 1000, "buying_power": 900, "net_assets": 1100}

    def positions(self, strict=True):
        return [{"symbol": "AAPL.US", "quantity": "2", "cost_price": "100",
                 "last_price": "105"}]

    def orders(self, strict=True):
        return [{"order_id": "o-1", "symbol": "AAPL.US", "side": "BUY",
                 "quantity": "2", "status": "FILLED"}]

    def static_info(self, symbol):
        return self.metadata

    def kline_by_count(self, symbol, count, period):
        self.kline_calls += 1
        return []


def test_status_degradation_does_not_refresh_successful_snapshot_time():
    conn = dbm.get_conn(":memory:")
    dbm.upsert_account(conn, "default", "SYNCED", cash=100)
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    dbm.set_account_updated_at(conn, "default", old_ts)

    dbm.set_account_sync_status(conn, "default", "STALE",
                                error_type="TimeoutError", error_message="offline")

    row = dbm.get_account(conn)
    assert row["updated_at"] == old_ts
    assert row["last_error_type"] == "TimeoutError"
    assert ensure_synced(conn).sync_status == "STALE"


def test_account_sync_persists_hydrated_snapshots_and_restores_them():
    conn = dbm.get_conn(":memory:")
    client = _AccountClient(_metadata("AAPL.US", metadata_version="v7"))
    result = sync_account(conn, client=client)

    assert result.sync_status == "SYNCED"
    assert result.positions[0]["symbol"] == "AAPL.US"
    assert result.metadata[0]["metadata"]["metadata_version"] == "v7"
    restored = load(conn)
    assert restored.positions[0]["quantity"] == 2
    assert restored.open_orders[0]["order_id"] == "o-1"
    assert dbm.list_sync_runs(conn, domain="account")[0]["status"] == "SYNCED"


def test_mismatch_is_not_cleared_by_asset_sync_and_recovers_only_after_reconcile():
    conn = dbm.get_conn(":memory:")
    dbm.upsert_account(conn, "default", "MISMATCH", cash=100)
    dbm.upsert_position(conn, "AAPL.US", 100, "2026-01-01", 2, 90, 110)
    client = _AccountClient(_metadata("AAPL.US"))

    account = sync_account(conn, client=client)
    assert account.sync_status == "MISMATCH"
    assert dbm.get_account(conn)["sync_status"] == "MISMATCH"
    result = sync_positions(conn, client=client)
    assert result["synced"] is True
    assert dbm.get_account(conn)["sync_status"] == "SYNCED"


def test_unknown_metadata_blocks_market_cache_before_provider_kline_call():
    conn = dbm.get_conn(":memory:")
    client = _AccountClient(_metadata("AAPL.US", asset_type="UNKNOWN"))

    with pytest.raises(HydrationRequiredError, match="metadata hydration failed"):
        cache_symbol(conn, "AAPL.US", count=630, client=client)

    assert client.kline_calls == 0
    assert dbm.get_manifest(conn, "AAPL.US") is None


def test_health_check_blocks_stale_or_unknown_account_and_audits_reason():
    conn = dbm.get_conn(":memory:")
    dbm.upsert_account(conn, "default", "UNKNOWN")
    report = health_check(conn, [], account_id="default")

    assert report["ok"] is False
    assert report["status"] == "BLOCKED"
    assert report["failures"][0]["status"] == "UNKNOWN"
    assert dbm.get_audit(conn, "account", "default")[0]["event"] == "MONITOR_HEALTH"


def test_provider_retry_is_bounded_and_only_retryable_errors_repeat():
    conn = dbm.get_conn(":memory:")
    provider = _Provider(NetworkError("offline"))
    result = SecurityService(conn, provider).ensure_batch(["AAPL.US"])[0]

    assert result["ok"] is False
    assert provider.calls == 2
    assert dbm.get_security(conn, "AAPL.US") is None
