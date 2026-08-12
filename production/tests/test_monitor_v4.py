import math

from production.monitor import (
    _completed_bar_freshness, _realtime_portfolio, intraday_check,
    pre_market_check, reset_alert_log,
)
from production.notification import AuditNotificationAdapter, Notification
from shared import db as dbm
from shared import longbridge_client as lb


def _fixture():
    conn = dbm.get_conn(":memory:")
    rows = []
    for i in range(120):
        close = 100 + i * 0.1 + math.sin(i / 10)
        rows.append({"ts": f"2025-01-{i + 1:03d}", "open": close,
                     "high": close + 1, "low": close - 1,
                     "close": close, "volume": 1_000_000})
    dbm.upsert_bars(conn, "TEST.US", rows, "test")
    params = {"entry_mode": "momentum", "ma_period": 20,
              "atr_multiple": 3.0, "buffer": 0.01}
    position = {"quantity": "10", "cost_price": "105"}
    return conn, params, position, rows[-1]["close"]


def test_pre_market_detects_missing_real_broker_protection():
    conn, params, position, _ = _fixture()
    report = pre_market_check(
        conn, "TEST.US", params, "2025-01-01", protective_orders=[],
        realtime_position=position,
    )
    assert report.position_open
    assert report.protective_missing


def test_intraday_detects_missing_protection_once_per_day():
    conn, params, position, price = _fixture()
    reset_alert_log("2025-01-01")
    first = intraday_check(
        conn, "TEST.US", params, "2025-01-01", price, price,
        realtime_position=position, protective_orders=[],
    )
    second = intraday_check(
        conn, "TEST.US", params, "2025-01-01", price, price,
        realtime_position=position, protective_orders=[],
    )
    assert any(a.condition == "missing_protective" for a in first)
    assert not any(a.condition == "missing_protective" for a in second)


def test_audit_notification_is_persisted():
    conn = dbm.get_conn(":memory:")
    adapter = AuditNotificationAdapter(conn)
    assert adapter.send(Notification("test", "title", "body"))
    rows = dbm.get_audit(conn)
    assert len(rows) == 1
    assert rows[0]["event"] == "NOTIFICATION"


def test_intraday_alert_dedupe_survives_new_process_connection(tmp_path):
    path = str(tmp_path / "alerts.db")
    first_conn = dbm.get_conn(path)
    _, params, position, price = _fixture()
    # 把 fixture 数据复制到文件库，模拟第一个 cron 进程。
    rows = [dict(row) for row in dbm.get_bars(_fixture()[0], "TEST.US")]
    dbm.upsert_bars(first_conn, "TEST.US", rows, "test")
    first = intraday_check(
        first_conn, "TEST.US", params, "2025-01-01", price, price,
        realtime_position=position, protective_orders=[],
    )
    first_conn.close()

    second_conn = dbm.get_conn(path)
    second = intraday_check(
        second_conn, "TEST.US", params, "2025-01-01", price, price,
        realtime_position=position, protective_orders=[],
    )
    assert any(a.condition == "missing_protective" for a in first)
    assert not any(a.condition == "missing_protective" for a in second)


def test_us_bar_freshness_uses_exchange_calendar_across_long_holiday():
    conn = dbm.get_conn(":memory:")
    dbm.upsert_calendar(conn, "US", [
        {"trade_date": "2026-12-24", "is_open": True},
        {"trade_date": "2026-12-25", "is_open": False},
        {"trade_date": "2026-12-26", "is_open": False},
        {"trade_date": "2026-12-27", "is_open": False},
        {"trade_date": "2026-12-28", "is_open": False},
        {"trade_date": "2026-12-29", "is_open": False},
        {"trade_date": "2026-12-30", "is_open": True},
        {"trade_date": "2026-12-31", "is_open": True},
    ], "test")
    # 亚洲时区运行允许 US 最新完成 bar 落后一个交易日；长假不能按自然日误判。
    assert _completed_bar_freshness(
        conn, "A.US", "2026-12-24", "2026-12-30")[0]
    # 再经过一个真实开市日后，12/24 已不新鲜。
    assert not _completed_bar_freshness(
        conn, "A.US", "2026-12-24", "2026-12-31")[0]


def test_longbridge_bar_without_calendar_fails_closed():
    conn = dbm.get_conn(":memory:")
    dbm.upsert_bars(conn, "A.US", [{
        "ts": "2026-08-08", "open": 100, "high": 101, "low": 99,
        "close": 100, "volume": 1_000_000,
    }], "longbridge")
    fresh, reason = _completed_bar_freshness(
        conn, "A.US", "2026-08-08", "2026-08-09")
    assert not fresh
    assert "交易日历未覆盖" in reason


def test_realtime_portfolio_default_client_has_trade_scope(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, *, scope):
            calls.append(scope)

        def positions(self):
            return [{"symbol": "AAPL.US", "quantity": "2"}]

    monkeypatch.setattr(lb, "LongbridgeClient", FakeClient)

    assert _realtime_portfolio() == [{"symbol": "AAPL.US", "quantity": "2"}]
    assert calls == ["trade"]
