"""v4 DataHub / PIT / historical Universe regression tests."""

import pytest

from shared import db as dbm


@pytest.fixture
def conn():
    return dbm.get_conn(":memory:")


def test_universe_snapshots_are_historical_and_immutable(conn):
    first = dbm.snapshot_universe(
        conn, "watchlist", ["A.US", "B.US"], as_of_date="2025-01-31")
    second = dbm.snapshot_universe(
        conn, "watchlist", ["B.US", "C.US"], as_of_date="2025-02-28")

    assert first != second
    assert [row["symbol"] for row in dbm.universe_as_of(
        conn, "2025-01-31", "watchlist")] == ["A.US", "B.US"]
    assert [row["symbol"] for row in dbm.universe_as_of(
        conn, "2025-02-28", "watchlist")] == ["B.US", "C.US"]
    assert dbm.snapshot_universe(
        conn, "watchlist", ["A.US", "B.US"], as_of_date="2025-01-31") == first
    with pytest.raises(ValueError, match="禁止覆盖"):
        dbm.snapshot_universe(
            conn, "watchlist", ["Z.US"], as_of_date="2025-01-31")


def test_fundamental_queries_are_point_in_time(conn):
    dbm.upsert_fundamental(
        conn, "A.US", "2024-12-31", "2025-02-01", "2025-02-02",
        {"eps": 1.0}, revision=0)
    dbm.upsert_fundamental(
        conn, "A.US", "2024-12-31", "2025-03-01", "2025-03-02",
        {"eps": 1.2}, revision=1)

    feb = dbm.fundamentals_as_of(conn, "A.US", "2025-02-15")
    mar = dbm.fundamentals_as_of(conn, "A.US", "2025-03-15")

    assert len(feb) == 1 and feb[0]["revision"] == 0
    assert len(mar) == 1 and mar[0]["revision"] == 1


def test_api_quota_reservation_is_fail_closed(conn):
    at = "2025-01-01T12:00:00Z"
    assert dbm.reserve_api_quota(
        conn, "quote", amount=2, quota_limit=3, at=at)["allowed"]
    rejected = dbm.reserve_api_quota(
        conn, "quote", amount=2, quota_limit=3, at=at)
    assert not rejected["allowed"]
    assert rejected["used"] == 2
