from shared import db as dbm
from shared.longbridge_client import LongbridgeClient, NetworkError
from shared.security import SecurityService


def _info(symbol, *, board, currency="USD", name="Company",
          total=1000, circulating=900, eps=2.0):
    fields = {
        "symbol": symbol, "board": board, "currency": currency,
        "exchange": "NASDAQ" if currency == "USD" else "HKEX",
        "lot_size": 1 if currency == "USD" else 100,
        "name_en": name, "name_cn": "", "name_hk": "",
        "hk_shares": 0, "stock_derivatives": [],
        "total_shares": total, "circulating_shares": circulating,
        "eps": eps, "eps_ttm": eps, "bps": 10.0,
        "dividend_yield": 0,
    }
    return type("StaticInfo", (), fields)()


class _UsMainEnum:
    name = "USMain"

    def __str__(self):
        return "SecurityBoard.USMain"


def test_us_main_and_us_pink_board_expressions_map_company_to_equity():
    assert LongbridgeClient._asset_type_from_static_info(
        _info("AAPL.US", board="SecurityBoard.USMain", name="AAPL")) == "EQUITY"
    assert LongbridgeClient._asset_type_from_static_info(
        _info("AMD.US", board=_UsMainEnum(), name="AMD")) == "EQUITY"
    assert LongbridgeClient._asset_type_from_static_info(
        _info("PINK.US", board="USPink", name="Pink Company")) == "EQUITY"


def test_us_main_does_not_require_total_shares_to_exceed_circulating_shares():
    info = _info("AAPL.US", board="USMain", name="Apple",
                 total=100, circulating=120)
    assert LongbridgeClient._asset_type_from_static_info(info) == "EQUITY"


def test_hk_equity_board_maps_to_equity_without_currency_guess():
    info = _info(
        "0700.HK", board="HKEquity", currency="HKD",
        name="Tencent")
    assert LongbridgeClient._asset_type_from_static_info(info) == "EQUITY"
    assert info.currency == "HKD"


def test_us_main_ambiguous_fund_types_remain_unknown():
    etf = _info("ETF.US", board="USMain", name="SPDR S&P 500 ETF Trust",
                 eps=0)
    leveraged = _info("LEV.US", board="USMain", name="Example Bull 3X",
                      eps=0)
    assert LongbridgeClient._asset_type_from_static_info(etf) == "UNKNOWN"
    assert LongbridgeClient._asset_type_from_static_info(leveraged) == "UNKNOWN"


def test_reit_option_and_warrant_are_never_plain_equity():
    reit = _info("REIT.US", board="USMain", name="Example REIT", eps=0)
    option = _info("OPT.US", board="SecurityBoard.USOption")
    option_s = _info("OPTS.US", board="USOptionS")
    warrant = _info("WAR.HK", board="SecurityBoard.HKWarrant", currency="HKD")
    assert LongbridgeClient._asset_type_from_static_info(reit) == "UNKNOWN"
    assert LongbridgeClient._asset_type_from_static_info(option) == "OPTION"
    assert LongbridgeClient._asset_type_from_static_info(option_s) == "OPTION"
    assert LongbridgeClient._asset_type_from_static_info(warrant) == "WARRANT"


def test_unknown_instrument_remains_unknown_despite_us_suffix():
    unknown = _info(
        "MYSTERY.US", board="Unknown", total=0,
        circulating=0, eps=0)
    assert LongbridgeClient._asset_type_from_static_info(unknown) == "UNKNOWN"


class _Provider:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def static_info(self, symbol):
        self.calls.append(symbol)
        response = self.responses[symbol]
        if isinstance(response, Exception):
            raise response
        return response


def _metadata(symbol, *, currency="USD", asset_type="EQUITY"):
    return {"symbol": symbol, "name": symbol, "exchange": "NASDAQ",
            "currency": currency, "asset_type": asset_type, "lot_size": 1}


def test_security_service_is_idempotent_and_cache_first():
    conn = dbm.get_core_conn(":memory:")
    provider = _Provider({"AAPL.US": _metadata("AAPL.US")})
    service = SecurityService(conn, provider)

    first = service.ensure_metadata("AAPL.US")
    second = service.ensure_metadata("AAPL.US")

    assert provider.calls == ["AAPL.US"]
    assert first["metadata_source"] == "provider"
    assert second["metadata_source"] == "security_master"


def test_provider_error_does_not_persist_metadata():
    conn = dbm.get_core_conn(":memory:")
    provider = _Provider({"FAIL.US": NetworkError("offline")})
    result = SecurityService(conn, provider).ensure_batch(["FAIL.US"])[0]

    assert result == {
        "symbol": "FAIL.US", "ok": False, "error_type": "NetworkError",
        "error_message": "offline", "retryable": True,
    }
    assert dbm.get_security(conn, "FAIL.US") is None


def test_batch_returns_per_symbol_failures_and_preserves_hkd():
    conn = dbm.get_core_conn(":memory:")
    provider = _Provider({
        "0700.HK": _metadata("0700.HK", currency="HKD"),
        "MYSTERY.US": _metadata("MYSTERY.US", asset_type="UNKNOWN"),
    })
    results = SecurityService(conn, provider).ensure_batch(
        ["0700.HK", "MYSTERY.US"])

    assert results[0]["ok"] is True
    assert dbm.get_security(conn, "0700.HK")["currency"] == "HKD"
    assert results[1]["ok"] is False
    assert results[1]["symbol"] == "MYSTERY.US"
    assert dbm.get_security(conn, "MYSTERY.US") is None
