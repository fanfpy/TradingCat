import json
from types import SimpleNamespace

import pytest

from shared.data_providers import (
    FundamentalProviderChain, FundamentalSnapshot, OpenAliceCommandProvider,
    ProviderCapabilities, validate_pit_snapshot,
)


def test_openalice_adapter_uses_json_stdio_without_shell_and_is_not_pit():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0, stderr="", stdout=json.dumps({"data": {"roe": 0.3}}))

    provider = OpenAliceCommandProvider(
        ["alice-bridge", "--safe"], runner=runner,
        clock=lambda: "2026-08-10T00:00:00Z")
    result = provider.current_snapshot("AAPL.US")
    assert calls[0][0] == ["alice-bridge", "--safe"]
    assert json.loads(calls[0][1]["input"])["symbol"] == "AAPL.US"
    assert "shell" not in calls[0][1]
    assert result.values == {"roe": 0.3}
    assert result.pit_safe is False


def test_provider_chain_degrades_per_source_and_preserves_lineage():
    class Broken:
        name = "broken"

        def current_snapshot(self, symbol):
            raise RuntimeError("offline")

    class Working:
        name = "working"

        def current_snapshot(self, symbol):
            return FundamentalSnapshot(
                symbol=symbol, source=self.name, observed_at="2026-08-10T00:00:00Z",
                values={"roe": 0.2},
                capabilities=ProviderCapabilities(True, False, False, False))

    result = FundamentalProviderChain([
        Broken(), Working()
    ]).current("AAPL.US")
    assert [item.source for item in result.snapshots] == ["working"]
    assert any("broken" in warning for warning in result.warnings)


def test_non_pit_current_payload_cannot_pass_pit_validation():
    with pytest.raises(ValueError, match="published_at"):
        validate_pit_snapshot({
            "period_end": "2025-12-31", "available_at": "2026-02-01",
            "values": {"revenue": 1}, "source": "openalice",
        })


def test_pit_validation_requires_explicit_three_times_and_source():
    row = {
        "period_end": "2025-12-31", "published_at": "2026-01-31",
        "available_at": "2026-02-01", "values": {"revenue": 1},
        "source": "licensed-pit-feed",
    }
    assert validate_pit_snapshot(row) is row


def test_fundamentals_cli_degrades_without_initializing_longbridge(monkeypatch, capsys):
    import tc

    monkeypatch.delenv("TRADINGCAT_OPENALICE_ADAPTER_COMMAND", raising=False)
    monkeypatch.setenv("TRADINGCAT_ENV_FILE", "/definitely/missing/tradingcat.env")
    args = SimpleNamespace(cmd="fundamentals", symbol="AAPL.US", json=True)

    assert tc.cmd_market(args) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["snapshots"] == []
    assert "没有可用" in payload["warnings"][-1]
