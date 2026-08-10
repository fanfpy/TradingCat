from datetime import date

from shared import db as dbm
from shared.datahub import LongbridgeDataHub


class FakeClient:
    def trading_calendar(self, market, start, end):
        return [{"trade_date": "2025-01-02", "is_open": True},
                {"trade_date": "2025-01-03", "is_open": False}]

def test_longbridge_datahub_syncs_calendar():
    conn = dbm.get_conn(":memory:")
    hub = LongbridgeDataHub(conn, FakeClient(), daily_quota=10)
    assert hub.sync_calendar("US", date(2025, 1, 2), date(2025, 1, 3)) == 2


def test_datahub_calendar_quota_fails_closed():
    conn = dbm.get_conn(":memory:")
    hub = LongbridgeDataHub(conn, FakeClient(), daily_quota=2)
    args = ("US", date(2025, 1, 2), date(2025, 1, 3))
    assert hub.sync_calendar(*args) == 2
    assert hub.sync_calendar(*args) == 2
    # 第三次请求在调用 client 前被 quota 拒绝。
    import pytest
    with pytest.raises(RuntimeError, match="quota"):
        hub.sync_calendar(*args)


def test_date_only_as_of_includes_same_day_available_timestamp():
    conn = dbm.get_conn(":memory:")
    dbm.append_fundamental_revision(
        conn, "AAA.US", "2024-12-31", "2025-02-01T08:00:00Z",
        "2025-02-02T12:30:00Z", {"revenue": 100}, source="longbridge")
    dbm.append_corporate_action_revision(
        conn, "AAA.US", "split", "2025-03-01",
        "2025-02-02T12:30:00Z", {"ratio": "2:1"}, source="longbridge")
    assert len(dbm.fundamentals_as_of(conn, "AAA.US", "2025-02-02")) == 1
    assert len(dbm.corporate_actions_as_of(conn, "AAA.US", "2025-02-02")) == 1
