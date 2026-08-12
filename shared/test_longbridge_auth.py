"""长桥 SDK 认证防回归：不读取 OAuth token，不启动 CLI。"""

import json
import os
import inspect
from datetime import date
from importlib.metadata import version
from pathlib import Path

import pytest

from shared import longbridge_client as lb
from shared.sdk_diagnostics import diagnose_longbridge


ENV_KEYS = (*lb.EnvironmentAdapter.ENV_KEYS, "TRADINGCAT_ENV_FILE")


def _clear_longbridge_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_credentials_never_fall_back_to_cli_oauth_token(monkeypatch, tmp_path):
    _clear_longbridge_env(monkeypatch)
    token_dir = tmp_path / ".longbridge" / "openapi" / "tokens"
    token_dir.mkdir(parents=True)
    (token_dir / "oauth.json").write_text(json.dumps({
        "client_id": "oauth-client",
        "client_secret": "oauth-secret",
        "access_token": "oauth-access-token",
    }), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    credentials = lb.EnvironmentAdapter.load_credentials(
        env_file=str(tmp_path / "missing.env"))

    assert credentials == {"app_key": "", "app_secret": "", "access_token": ""}


def test_env_file_loads_sdk_credentials_and_official_endpoint(monkeypatch, tmp_path):
    _clear_longbridge_env(monkeypatch)
    env_file = tmp_path / "sdk.env"
    env_file.write_text(
        "LONGBRIDGE_APP_KEY=app-key\n"
        "LONGBRIDGE_APP_SECRET='app-secret'\n"
        'LONGBRIDGE_ACCESS_TOKEN="legacy-token"\n'
        "LONGBRIDGE_HTTP_URL=https://openapi.example.test\n",
        encoding="utf-8",
    )

    credentials = lb.EnvironmentAdapter.load_credentials(env_file=str(env_file))

    assert credentials == {
        "app_key": "app-key",
        "app_secret": "app-secret",
        "access_token": "legacy-token",
    }
    assert os.environ["LONGBRIDGE_HTTP_URL"] == "https://openapi.example.test"


def test_client_requires_all_three_legacy_credentials(monkeypatch, tmp_path):
    _clear_longbridge_env(monkeypatch)
    monkeypatch.setenv("TRADINGCAT_ENV_FILE", str(tmp_path / "missing.env"))

    with pytest.raises(RuntimeError, match="LONGBRIDGE_APP_SECRET"):
        lb.LongbridgeClient(app_key="app-key", access_token="legacy-token")


def test_client_uses_sdk_apikey_factory(monkeypatch):
    _clear_longbridge_env(monkeypatch)
    calls = []

    class FactoryConfig:
        @classmethod
        def from_apikey(cls, **kwargs):
            calls.append(kwargs)
            return cls()

    class FakeContext:
        def __init__(self, config):
            assert isinstance(config, FactoryConfig)

    monkeypatch.setattr(lb, "_load_sdk", lambda: True)
    monkeypatch.setattr(lb, "_Config", FactoryConfig)
    monkeypatch.setattr(lb, "_QuoteContext", FakeContext)
    monkeypatch.setattr(lb, "_TradeContext", FakeContext)

    client = lb.LongbridgeClient(
        app_key="app-key", app_secret="app-secret", access_token="legacy-token")

    assert isinstance(client._config, FactoryConfig)
    assert calls == [{"app_key": "app-key", "app_secret": "app-secret",
                      "access_token": "legacy-token"}]


def test_installed_longbridge_443_exposes_legacy_and_boundary_methods():
    import longbridge.openapi as sdk

    assert version("longbridge") == "4.4.3"
    assert callable(sdk.Config.from_apikey)
    assert list(inspect.signature(sdk.Config.from_apikey).parameters)[:3] == [
        "app_key", "app_secret", "access_token"]
    assert callable(sdk.QuoteContext.quote)
    assert callable(sdk.QuoteContext.static_info)
    assert callable(sdk.QuoteContext.trading_days)
    assert callable(sdk.TradeContext.submit_order)
    assert callable(sdk.TradeContext.account_balance)
    assert callable(sdk.TradeContext.stock_positions)


def test_client_adapts_v443_quote_account_positions_and_calendar(monkeypatch):
    calls = []
    trade_calls = []

    class FakeQuoteContext:
        def __init__(self, config):
            self.config = config

        def quote(self, symbols):
            return [type("SecurityQuote", (), {
                "symbol": symbols[0], "last_done": "101.5", "prev_close": "100",
                "high": "102", "low": "99", "open": "100.5", "volume": 12,
                "turnover": "1218", "timestamp": "2026-08-11 10:00:00",
            })()]

        def static_info(self, symbols):
            return [type("SecurityStaticInfo", (), {
                "symbol": symbols[0], "name_en": "Apple", "name_cn": "",
                "name_hk": "", "exchange": "NASDAQ", "currency": "USD",
                "lot_size": 1, "board": "USMain", "total_shares": 10,
                "circulating_shares": 9, "eps": "2.5", "eps_ttm": "3.5",
                "bps": "4.5", "dividend_yield": "0.25",
            })()]

        def trading_days(self, market, begin, end):
            calls.append((market, begin, end))
            return type("MarketTradingDays", (), {
                "trading_days": [begin], "half_trading_days": [end]
            })()

    class FakeTradeContext:
        def __init__(self, config):
            self.config = config

        def account_balance(self):
            return [type("AccountBalance", (), {
                "total_cash": "1000", "max_finance_amount": "250",
                "net_assets": "1200", "currency": "USD",
            })()]

        def stock_positions(self):
            return type("StockPositionsResponse", (), {"channels": [
                type("StockPositionChannel", (), {
                    "account_channel": "US", "positions": [
                        type("StockPosition", (), {
                            "symbol": "AAPL.US", "quantity": "2",
                            "available_quantity": "2", "cost_price": "90",
                            "currency": "USD",
                        })()
                    ]
                })()
            ]})()

        def submit_order(self, **kwargs):
            trade_calls.append(kwargs)
            return type("SubmitOrderResponse", (), {
                "order_id": "dry-run-order", "status": "Submitted"
            })()

    class FakeConfig:
        @classmethod
        def from_apikey(cls, **kwargs):
            return kwargs

    class FakeMarket:
        US = "US"

    monkeypatch.setattr(lb, "_load_sdk", lambda: True)
    monkeypatch.setattr(lb, "_Config", FakeConfig)
    monkeypatch.setattr(lb, "_QuoteContext", FakeQuoteContext)
    monkeypatch.setattr(lb, "_TradeContext", FakeTradeContext)
    monkeypatch.setattr(lb, "_Market", FakeMarket)

    client = lb.LongbridgeClient(
        app_key="app-key", app_secret="app-secret", access_token="legacy-token")

    assert client.quote("AAPL.US")["current_price"] == 101.5
    assert client.static_info("AAPL.US")["dividend_per_share"] == 0.25
    assert client.assets() == {
        "total_cash": "1000.0", "max_finance_amount": "250.0",
        "net_assets": "1200.0", "currency": "USD",
    }
    assert client.positions()[0]["symbol"] == "AAPL.US"
    assert client.order("buy", "AAPL.US", 2, price=101.5)["success"] is True
    assert trade_calls[0]["submitted_quantity"] == 2
    rows = client.trading_calendar("US", date(2026, 1, 1), date(2026, 2, 15))
    assert len(calls) == 2
    assert rows[0] == {"trade_date": "2026-01-01", "is_open": True, "half_day": False}
    assert rows[-1] == {"trade_date": "2026-02-15", "is_open": True, "half_day": True}


def test_quote_and_trade_contexts_are_structurally_isolated(monkeypatch):
    created = []

    class FakeConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        @classmethod
        def from_apikey(cls, **kwargs):
            return cls(**kwargs)

    class QuoteContext:
        def __init__(self, config):
            created.append("quote")

    class TradeContext:
        def __init__(self, config):
            created.append("trade")

    monkeypatch.setattr(lb, "_load_sdk", lambda: True)
    monkeypatch.setattr(lb, "_Config", FakeConfig)
    monkeypatch.setattr(lb, "_QuoteContext", QuoteContext)
    monkeypatch.setattr(lb, "_TradeContext", TradeContext)
    kwargs = {"app_key": "key", "app_secret": "secret",
              "access_token": "token"}

    quote = lb.LongbridgeClient(scope="quote", **kwargs)
    assert created == ["quote"]
    assert quote._trade_ctx is None
    created.clear()
    trade = lb.LongbridgeClient(scope="trade", **kwargs)
    assert created == ["trade"]
    assert trade._quote_ctx is None


def test_strict_separate_credentials_fail_closed(monkeypatch, tmp_path):
    _clear_longbridge_env(monkeypatch)
    monkeypatch.setenv("TRADINGCAT_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("TRADINGCAT_REQUIRE_SEPARATE_CREDENTIALS", "1")
    with pytest.raises(RuntimeError, match="凭证隔离"):
        lb.EnvironmentAdapter.load_credentials(scope="trade")


def test_both_scope_is_rejected_when_separate_credentials_are_required(monkeypatch):
    _clear_longbridge_env(monkeypatch)
    monkeypatch.setenv("TRADINGCAT_REQUIRE_SEPARATE_CREDENTIALS", "1")
    monkeypatch.setenv("LONGBRIDGE_APP_KEY", "app-key")
    monkeypatch.setenv("LONGBRIDGE_APP_SECRET", "app-secret")
    monkeypatch.setenv("LONGBRIDGE_ACCESS_TOKEN", "legacy-token")

    with pytest.raises(RuntimeError, match="scope='quote'.*scope='trade'"):
        lb.EnvironmentAdapter.load_credentials(scope="both")


def test_non_empty_positions_do_not_require_quote_context():
    client = object.__new__(lb.LongbridgeClient)

    class TradeContext:
        def stock_positions(self):
            return type("Response", (), {"channels": [
                type("Channel", (), {
                    "account_channel": "US",
                    "positions": [type("Position", (), {
                        "symbol": "AAPL.US", "quantity": "2",
                        "available_quantity": "2", "cost_price": "90",
                        "currency": "USD",
                    })()],
                })(),
            ]})()

    class FailingQuoteContext:
        def quote(self, symbols):
            raise AssertionError("positions() must not call QuoteContext")

    client._trade_ctx = TradeContext()
    client._quote_ctx = FailingQuoteContext()

    positions = client.positions()

    assert positions == [{
        "symbol": "AAPL.US", "quantity": "2", "available_quantity": "2",
        "cost_price": "90.0", "currency": "USD", "market": "US",
    }]


def test_client_preserves_sdk_endpoint_with_scoped_context(monkeypatch, tmp_path):
    _clear_longbridge_env(monkeypatch)
    env_file = tmp_path / "sdk.env"
    env_file.write_text(
        "LONGBRIDGE_APP_KEY=app-key\n"
        "LONGBRIDGE_APP_SECRET=app-secret\n"
        "LONGBRIDGE_ACCESS_TOKEN=legacy-token\n"
        "LONGBRIDGE_HTTP_URL=https://openapi.example.test\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADINGCAT_ENV_FILE", str(env_file))
    calls = []

    class CurrentConfig:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        @classmethod
        def from_apikey(cls, **kwargs):
            return cls(**kwargs)

    class FakeContext:
        def __init__(self, config):
            assert isinstance(config, CurrentConfig)

    monkeypatch.setattr(lb, "_load_sdk", lambda: True)
    monkeypatch.setattr(lb, "_Config", CurrentConfig)
    monkeypatch.setattr(lb, "_QuoteContext", FakeContext)
    monkeypatch.setattr(lb, "_TradeContext", FakeContext)

    client = lb.LongbridgeClient(scope="quote")

    assert isinstance(client._config, CurrentConfig)
    assert calls[0]["http_url"] == "https://openapi.example.test"
    assert calls[0]["app_key"] == "app-key"


def test_v4_static_info_normalizes_list_and_dividend_semantics(monkeypatch):
    class FakeConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        @classmethod
        def from_apikey(cls, **kwargs):
            return cls(**kwargs)

    class QuoteContext:
        def __init__(self, config):
            self.config = config

        def static_info(self, symbols):
            return [type("Info", (), {
                "symbol": symbols[0], "name_en": "Apple", "name_cn": "",
                "name_hk": "", "exchange": "NASDAQ", "currency": "USD",
                "lot_size": 1, "total_shares": 10, "circulating_shares": 9,
                "eps": "2.5", "eps_ttm": "3.5", "bps": "4.5",
                # SDK v4 的字段名误导，文档定义实际是每股股息。
                "dividend_yield": "0.25",
            })()]

    monkeypatch.setattr(lb, "_load_sdk", lambda: True)
    monkeypatch.setattr(lb, "_Config", FakeConfig)
    monkeypatch.setattr(lb, "_QuoteContext", QuoteContext)
    monkeypatch.setattr(lb, "_TradeContext", QuoteContext)
    client = lb.LongbridgeClient(
        app_key="key", app_secret="secret", access_token="legacy", scope="quote")
    info = client.static_info("AAPL.US")
    assert info["symbol"] == "AAPL.US"
    assert info["eps_ttm"] == 3.5
    assert info["dividend_per_share"] == 0.25
    assert "dividend_yield" not in info


def test_production_paths_do_not_spawn_longbridge_cli():
    root = Path(__file__).resolve().parents[1]
    for relative in ("tc.py", "research/pipeline.py", "shared/longbridge_client.py"):
        source = (root / relative).read_text(encoding="utf-8")
        assert '["longbridge"' not in source
        assert "['longbridge'" not in source


def test_sdk_diagnostics_supports_credential_free_offline_validation(
        monkeypatch, tmp_path):
    _clear_longbridge_env(monkeypatch)
    monkeypatch.setenv("TRADINGCAT_ENV_FILE", str(tmp_path / "missing.env"))

    offline = diagnose_longbridge(connect=False, require_credentials=False)
    credential_check = diagnose_longbridge(connect=False, require_credentials=True)

    assert offline["credentials_required"] is False
    assert offline["passed"] is True
    assert credential_check["credentials_required"] is True
    assert credential_check["passed"] is False


@pytest.mark.parametrize(
    ("message", "error_type", "retryable"),
    [
        ("401 unauthorized token", lb.AuthenticationError, False),
        ("connection timed out", lb.NetworkError, True),
    ],
)
def test_quote_failures_are_not_disguised_as_empty_data(
        message, error_type, retryable):
    client = object.__new__(lb.LongbridgeClient)

    class FailingQuoteContext:
        def quote(self, symbols):
            raise RuntimeError(message)

    client._quote_ctx = FailingQuoteContext()

    with pytest.raises(error_type) as caught:
        client.quote("0700.HK")
    assert caught.value.retryable is retryable


def test_explicit_ui_degrade_can_return_empty_quote():
    client = object.__new__(lb.LongbridgeClient)

    class FailingQuoteContext:
        def quote(self, symbols):
            raise TimeoutError("network timeout")

    client._quote_ctx = FailingQuoteContext()
    assert client.quote("0700.HK", strict=False) is None


def test_segmented_kline_failure_never_returns_partial_history():
    client = object.__new__(lb.LongbridgeClient)

    class FailingQuoteContext:
        def history_candlesticks_by_date(self, **kwargs):
            raise TimeoutError("network timeout")

    client._quote_ctx = FailingQuoteContext()
    with pytest.raises(lb.NetworkError):
        client._kline_by_sdk_years("AAPL.US", 1500, object(), object(), "day")


def test_quote_derives_change_when_sdk_fields_are_absent():
    quote = type("Quote", (), {
        "symbol": "AAPL.US", "last_done": 98, "prev_close": 100,
        "name": "", "high": 101, "low": 97, "open": 100,
        "volume": 1, "turnover": 98, "timestamp": "now",
    })()
    normalized = object.__new__(lb.LongbridgeClient)._normalize_quote(quote)
    assert normalized["change_value"] == -2
    assert normalized["change_pct"] == -2
