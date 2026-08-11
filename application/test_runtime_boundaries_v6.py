"""v6 Phase 1 runtime ownership regression tests."""

import ast
from pathlib import Path

import pytest

from application.contracts import TradingCatApplication
from production.position import PositionIntent
from production.target_portfolio import build_target_portfolio
from shared import db as dbm
from shared.security import UnknownSecurityMetadataError


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = ("application", "production", "research", "execution")


def _production_files():
    for root_name in PRODUCTION_ROOTS:
        for path in (ROOT / root_name).rglob("*.py"):
            if "tests" in path.parts or path.name.startswith("test_"):
                continue
            yield path
    yield ROOT / "tc.py"


def test_production_code_does_not_call_ambiguous_get_conn():
    violations = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if (isinstance(function, ast.Attribute)
                    and isinstance(function.value, ast.Name)
                    and function.value.id == "dbm"
                    and function.attr == "get_conn"):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == [], "ambiguous dbm.get_conn calls: " + ", ".join(violations)


def test_unknown_hk_security_is_not_persisted_as_usd():
    core_conn = dbm.get_core_conn(":memory:")
    app = TradingCatApplication(core_conn)

    result = app.resolve_security("9999.HK")

    assert not result["ok"]
    assert result["error"]["code"] == "UNKNOWN_METADATA"
    assert dbm.get_security(core_conn, "9999.HK") is None


def test_longbridge_metadata_is_persisted_without_guessing():
    class MetadataProvider:
        def static_info(self, symbol):
            return {"symbol": symbol, "name": "Known HK", "exchange": "HKEX",
                    "currency": "HKD", "asset_type": "EQUITY", "lot_size": 100}

    core_conn = dbm.get_core_conn(":memory:")
    app = TradingCatApplication(core_conn, security_provider=MetadataProvider())

    result = app.resolve_security("9999.HK")

    assert result["ok"]
    stored = dbm.get_security(core_conn, "9999.HK")
    assert stored["currency"] == "HKD"
    assert stored["lot_size"] == 100


def test_ambiguous_provider_asset_type_remains_unknown():
    class AmbiguousProvider:
        def static_info(self, symbol):
            return {"symbol": symbol, "name": "US Main", "exchange": "NASD",
                    "currency": "USD", "asset_type": "UNKNOWN", "lot_size": 1}

    core_conn = dbm.get_core_conn(":memory:")
    app = TradingCatApplication(core_conn, security_provider=AmbiguousProvider())
    result = app.resolve_security("SPY.US")

    assert result["error"]["code"] == "UNKNOWN_METADATA"
    assert dbm.get_security(core_conn, "SPY.US") is None


def test_unknown_metadata_cannot_enter_portfolio_risk():
    core_conn = dbm.get_core_conn(":memory:")
    intent = PositionIntent(
        "9999.HK", "BUY", 0.01, entry_price=10.0, stop_price=9.0)

    with pytest.raises(UnknownSecurityMetadataError, match="UNKNOWN_METADATA"):
        build_target_portfolio(core_conn, 100_000, [intent])