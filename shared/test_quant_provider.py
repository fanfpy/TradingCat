import json
from types import SimpleNamespace

import pytest

from shared.quant_provider import LongbridgeQuantProvider


def test_missing_or_old_cli_is_clean_optional_capability():
    missing = LongbridgeQuantProvider(which=lambda _: None).capability()
    assert missing.available is False
    assert missing.final_validation is False

    def old_runner(argv, **kwargs):
        if argv[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="longbridge 0.16.1", stderr="")
        return SimpleNamespace(returncode=2, stdout="", stderr="unrecognized quant")

    old = LongbridgeQuantProvider(
        which=lambda _: "/usr/bin/longbridge", runner=old_runner).capability()
    assert old.available is False
    assert "不支持 quant run" in old.reason


def test_quant_preview_uses_argv_parses_nested_report_and_stays_research_only():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="longbridge 0.99", stderr="")
        if argv[-1] == "--help":
            return SimpleNamespace(returncode=0, stdout="help", stderr="")
        report = {"performanceAll": {"netProfitPercent": 12.3},
                  "closedTrades": []}
        return SimpleNamespace(
            returncode=0, stderr="",
            stdout=json.dumps({"report_json": json.dumps(report)}))

    provider = LongbridgeQuantProvider(
        which=lambda _: "/usr/bin/longbridge", runner=runner)
    result = provider.run_script(
        "AAPL.US", "2025-01-01", "2026-01-01", 'strategy("x");')
    run_argv, run_kwargs = calls[-1]
    assert run_argv[:4] == ["/usr/bin/longbridge", "quant", "run", "AAPL.US"]
    assert run_argv[-2:] == ["--script", 'strategy("x");']
    assert "shell" not in run_kwargs
    assert run_kwargs["encoding"] == "utf-8"
    assert run_kwargs["errors"] == "replace"
    assert result.performance["netProfitPercent"] == 12.3
    assert result.research_only is True


def test_quant_failure_does_not_fall_back_to_native_or_hide_oauth_requirement():
    def runner(argv, **kwargs):
        if argv[-1] in ("--version", "--help"):
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="not authenticated")

    provider = LongbridgeQuantProvider(
        which=lambda _: "/usr/bin/longbridge", runner=runner)
    with pytest.raises(RuntimeError, match="OAuth"):
        provider.run_script("AAPL.US", "2025-01-01", "2026-01-01", "x")
