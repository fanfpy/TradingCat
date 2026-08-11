#!/usr/bin/env python3
"""TradingCat JSON CLI 参考适配器；stdin/stdout 均为稳定 JSON。"""

import argparse
import json
import sys

from application.contracts import TradingCatApplication
from shared import db as dbm
from shared.data_providers import (
    FundamentalProviderChain, OpenAliceCommandProvider,
)
from shared.security import LazyLongbridgeSecurityProvider


OPERATIONS = {
    "analyze-security": "analyze_security",
    "follow-security": "follow_security",
    "review-portfolio": "review_portfolio",
    "propose-trade": "propose_trade",
    "explain-decision": "explain_decision",
    "request-approval": "request_approval",
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tradingcat-json")
    parser.add_argument("operation", choices=sorted(OPERATIONS))
    parser.add_argument("--input", default="-", help="JSON 文件；- 表示 stdin")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read() if args.input == "-" else open(
            args.input, encoding="utf-8").read()
        payload = json.loads(raw or "{}")
        core = dbm.get_core_conn()
        execution = dbm.get_execution_conn() if args.operation == "request-approval" else None
        providers = []
        openalice = OpenAliceCommandProvider.from_env()
        if openalice is not None:
            providers.append(openalice)
        app = TradingCatApplication(
            core, execution, fundamental_provider=FundamentalProviderChain(providers),
            security_provider=LazyLongbridgeSecurityProvider())
        result = getattr(app, OPERATIONS[args.operation])(**payload)
    except Exception as exc:
        result = {
            "schema_version": "tradingcat.v1", "operation": args.operation,
            "ok": False, "data": None,
            "error": {"code": "INTERNAL_ERROR", "message": str(exc)},
            "warnings": [], "lineage": {},
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
