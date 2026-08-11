from pathlib import Path

from application.golden_path import run_offline_golden_path


def test_v6_offline_golden_path_reaches_notification_outbox():
    result = run_offline_golden_path()

    assert result["ok"] is True
    assert result["offline"] is True
    assert [item["stage"] for item in result["stages"]] == [
        "candidate", "cache_bars", "prefilter", "research", "monitor_post",
        "notification_outbox", "runtime_isolation",
    ]
    assert all(item["status"] == "ok" for item in result["stages"])
    assert result["stages"][-1]["data"]["execution_plan_count"] == 0


def test_golden_path_has_no_execution_or_longbridge_dependency():
    source = (Path(__file__).with_name("golden_path.py")).read_text(encoding="utf-8")
    assert "from execution" not in source
    assert "LongbridgeClient" not in source