import json

from application import cli
from application.contracts import TradingCatApplication
from shared import db as dbm


def test_unknown_operation_is_a_machine_readable_argument_error(capsys):
    assert cli.main(["not-a-real-operation"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["operation"] == "not-a-real-operation"
    assert result["error"]["code"] == "UNKNOWN_OPERATION"


def test_invalid_json_is_a_machine_readable_argument_error(capsys, monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", type("Input", (), {
        "read": lambda self: "{bad-json",
    })())
    assert cli.main(["status"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["operation"] == "Status"
    assert result["error"]["code"] == "ARGUMENT_ERROR"


def test_short_status_and_report_operations_use_the_same_envelope(monkeypatch):
    conn = dbm.get_conn(":memory:")
    monkeypatch.setattr(cli.dbm, "get_core_conn", lambda: conn)
    status = cli._invoke("status", {})
    report = cli._invoke("report", {})
    for result, operation in ((status, "Status"), (report, "Report")):
        assert result["schema_version"] == "tradingcat.v1"
        assert result["operation"] == operation
        assert result["ok"] is True
        assert set(result) == {
            "schema_version", "request_id", "operation", "ok", "data",
            "error", "warnings", "lineage",
        }
    assert conn.execute("SELECT count(*) FROM security_master").fetchone()[0] == 0


def test_propose_live_creates_pending_approval_plan():
    app = TradingCatApplication(dbm.get_core_conn(":memory:"))
    result = app.propose_trade(100_000, mode="LIVE")
    assert result["ok"] is True
    assert result["data"]["status"] == "PENDING_APPROVAL"
    assert result["data"]["execution_plan"]["execution_mode"] == "LIVE"


def test_status_exposes_paper_default_and_live_pending_only():
    result = TradingCatApplication(dbm.get_core_conn(":memory:")).status()
    assert result["data"]["safety"] == {
        "default_mode": "PAPER", "paper_is_local": True,
        "live_enabled": False, "live_submission": "PENDING_APPROVAL_ONLY",
    }
