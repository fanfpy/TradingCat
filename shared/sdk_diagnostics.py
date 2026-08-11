"""Longbridge SDK 能力/凭证诊断；绝不调用 OAuth，也绝不提交订单。"""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys
from typing import Dict

from shared.longbridge_client import LONG_BRIDGE_SDK_VERSION

# 允许既作为模块导入，也从项目根目录直接执行本文件。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def diagnose_longbridge(*, connect: bool = False,
                        quote_symbol: str = "AAPL.US",
                        require_credentials: bool = True) -> Dict:
    result = {
        "sdk": "longbridge", "required_version": LONG_BRIDGE_SDK_VERSION,
        "installed": False, "version": None, "credentials": {},
        "capabilities": {}, "connectivity": "NOT_RUN",
        "fundamental_connectivity": "NOT_RUN",
        "credentials_required": require_credentials,
        "warnings": [], "passed": False,
    }
    try:
        installed = version("longbridge")
        import longbridge.openapi as sdk
    except (PackageNotFoundError, ImportError) as exc:
        result["warnings"].append(f"longbridge SDK unavailable: {exc}")
        return result

    result["installed"] = True
    result["version"] = installed
    config_type = getattr(sdk, "Config", None)
    quote_type = getattr(sdk, "QuoteContext", None)
    trade_type = getattr(sdk, "TradeContext", None)
    result["capabilities"] = {
        "api_key_config": callable(getattr(config_type, "from_apikey", None)),
        "quote": hasattr(quote_type, "quote"),
        "trade": hasattr(trade_type, "submit_order"),
        "static_info": hasattr(quote_type, "static_info"),
        "market_calendar": hasattr(quote_type, "trading_days"),
        "account": hasattr(trade_type, "account_balance"),
        "positions": hasattr(trade_type, "stock_positions"),
        "fundamental_context": hasattr(sdk, "FundamentalContext"),
        "financial_report": hasattr(
            getattr(sdk, "FundamentalContext", object), "financial_report"),
        "valuation": hasattr(getattr(sdk, "FundamentalContext", object), "valuation"),
        "corporate_actions": hasattr(
            getattr(sdk, "FundamentalContext", object), "corp_action"),
    }
    from shared.longbridge_client import EnvironmentAdapter
    for scope in ("quote", "trade"):
        creds = EnvironmentAdapter.load_credentials(scope=scope)
        # 只返回完整性布尔值，绝不回显任何凭证内容。
        result["credentials"][scope] = all(
            value and not value.startswith("your_") for value in creds.values())

    if not result["capabilities"]["fundamental_context"]:
        result["warnings"].append(
            f"longbridge {installed} 无 FundamentalContext；基本面模块必须显示数据缺失，"
            "可选 current-only Provider 只能补充当前分析，禁止回填历史 PIT")

    capabilities_ok = (
        installed == result["required_version"]
        and result["capabilities"]["api_key_config"]
        and result["capabilities"]["quote"]
        and result["capabilities"]["trade"]
        and result["capabilities"]["static_info"]
        and result["capabilities"]["market_calendar"]
        and result["capabilities"]["account"]
        and result["capabilities"]["positions"]
    )
    credentials_ok = result["credentials"]["quote"] or not require_credentials
    base_ok = capabilities_ok and credentials_ok
    if connect and base_ok:
        try:
            from shared.longbridge_client import LongbridgeClient
            client = LongbridgeClient(scope="quote")
            quote = client.quote(quote_symbol)
            result["connectivity"] = "PASS" if quote else "EMPTY"
            if result["capabilities"]["fundamental_context"]:
                company = client.company_profile(quote_symbol)
                result["fundamental_connectivity"] = "PASS" if company else "EMPTY"
            else:
                result["fundamental_connectivity"] = "UNSUPPORTED_SAFE_DEGRADE"
        except Exception as exc:
            if result["connectivity"] == "NOT_RUN":
                result["connectivity"] = "FAIL"
            result["fundamental_connectivity"] = "FAIL"
            result["warnings"].append(f"SDK read-only connectivity failed: {exc}")
    result["passed"] = base_ok and (not connect or result["connectivity"] == "PASS")
    return result


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Longbridge SDK 只读诊断")
    parser.add_argument("--connect", action="store_true", help="执行一次只读 AAPL 行情查询")
    parser.add_argument("--symbol", default="AAPL.US")
    args = parser.parse_args()
    report = diagnose_longbridge(connect=args.connect, quote_symbol=args.symbol)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)
