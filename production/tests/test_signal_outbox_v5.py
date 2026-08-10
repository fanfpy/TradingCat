import sqlite3

import pytest

from production.notification import dispatch_signal_outbox
from shared import db as dbm


class RecordingAdapter:
    def __init__(self, succeed=True):
        self.succeed = succeed
        self.items = []

    def send(self, notification):
        self.items.append(notification)
        return self.succeed


def _record(conn, symbol="A.US", payload=None, channels=None):
    return dbm.record_signal_with_outbox(
        conn, account_id="default", symbol=symbol, strategy_version_id=7,
        bar_ts="2026-08-08", signal_type="ENTRY",
        payload=payload or {"symbol": symbol, "kind": "ENTRY", "rationale": "test"},
        channels=channels,
    )


def test_signal_and_each_channel_outbox_are_idempotent():
    conn = dbm.get_conn(":memory:")
    first = _record(conn, channels=["wechat", "audit"])
    second = _record(conn, channels=["audit", "wechat"])
    assert first["created"] is True
    assert second["created"] is False
    assert conn.execute("SELECT count(*) FROM signal_event").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM notification_outbox").fetchone()[0] == 2


def test_event_and_outbox_rollback_together_on_outbox_failure():
    conn = dbm.get_conn(":memory:")
    conn.execute(
        "CREATE TRIGGER reject_outbox BEFORE INSERT ON notification_outbox "
        "BEGIN SELECT RAISE(ABORT, 'injected outbox failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        _record(conn)
    assert conn.execute("SELECT count(*) FROM signal_event").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM notification_outbox").fetchone()[0] == 0


def test_same_signal_key_has_immutable_payload():
    conn = dbm.get_conn(":memory:")
    _record(conn)
    with pytest.raises(RuntimeError, match="immutable payload mismatch"):
        _record(conn, payload={"symbol": "A.US", "kind": "ENTRY", "rationale": "changed"})
    assert conn.execute("SELECT count(*) FROM signal_event").fetchone()[0] == 1


def test_outbox_worker_marks_success_and_retries_failure():
    conn = dbm.get_conn(":memory:")
    _record(conn)
    failed_adapter = RecordingAdapter(succeed=False)
    first = dispatch_signal_outbox(conn, failed_adapter)
    assert first == {"processed": 1, "sent": 0, "failed": 1}
    row = dbm.list_notification_outbox(conn, status="FAILED_RETRYABLE")[0]
    assert row["attempts"] == 1

    success_adapter = RecordingAdapter(succeed=True)
    second = dispatch_signal_outbox(conn, success_adapter)
    assert second == {"processed": 1, "sent": 1, "failed": 0}
    row = dbm.list_notification_outbox(conn, status="SENT")[0]
    assert row["attempts"] == 2

