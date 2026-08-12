#!/usr/bin/env python3
"""Stable JSON CLI contract for OpenClaw/Codex integrations."""

import argparse
import json
import sys
from typing import Any, Dict

from application.contracts import SCHEMA_VERSION, TradingCatApplication
from shared import db as dbm
from shared.data_providers import FundamentalProviderChain, OpenAliceCommandProvider
from shared.security import LazyLongbridgeSecurityProvider


OPERATIONS = {
    "analyze": ("analyze_security", "Analyze"),
    "backtest": ("backtest", "Backtest"),
    "propose": ("propose_trade", "Propose"),
    "paper": ("propose_trade", "Paper"),
    "status": ("status", "Status"),
    "report": ("report", "Report"),
    "analyze-security": ("analyze_security", "AnalyzeSecurity"),
    "follow-security": ("follow_security", "FollowSecurity"),
    "review-portfolio": ("review_portfolio", "ReviewPortfolio"),
    "propose-trade": ("propose_trade", "ProposeTrade"),
    "explain-decision": ("explain_decision", "ExplainDecision"),
    "request-approval": ("request_approval", "RequestApproval"),
    "approve": ("approve", "Approve"),
    "execute": ("execute", "Execute"),
}


class ContractArgumentError(ValueError):
    """An argv or input error rendered as a JSON contract."""


class ContractArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ContractArgumentError(message)


def _envelope(operation: str, *, data=None, error=None, warnings=None,
              lineage=None) -> Dict[str, Any]:
    import uuid
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": f"req_{uuid.uuid4().hex[:12]}",
        "operation": operation,
        "ok": error is None,
        "data": data,
        "error": error,
        "warnings": list(warnings or []),
        "lineage": dict(lineage or {}),
    }


def _error(operation: str, code: str, message: str, *, retryable=False):
    return _envelope(operation, error={
        "code": code, "message": message, "retryable": retryable,
    })


def _parse_args(argv=None):
    parser = ContractArgumentParser(prog="tradingcat-json")
    parser.add_argument("operation", nargs="?", help="稳定操作名；使用 --help 查看")
    parser.add_argument("--input", default="-", help="JSON 文件；- 表示 stdin")
    return parser.parse_args(argv)


def _read_payload(path: str) -> Dict[str, Any]:
    if path == "-":
        raw = sys.stdin.read()
    else:
        try:
            with open(path, encoding="utf-8") as stream:
                raw = stream.read()
        except OSError as exc:
            raise ContractArgumentError(f"无法读取 JSON 输入文件: {path}: {exc}") from exc
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ContractArgumentError(
            f"JSON 无效: {exc.msg} (line {exc.lineno}, column {exc.colno})") from exc
    if not isinstance(payload, dict):
        raise ContractArgumentError("JSON 请求必须是 object")
    return payload


def _invoke(operation: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    target, envelope_operation = OPERATIONS[operation]
    if operation == "execute":
        allowed = {"plan_id", "confirmation_id"}
        unexpected = sorted(set(payload) - allowed)
        missing = sorted(allowed - set(payload))
        if unexpected or missing:
            details = []
            if missing:
                details.append("缺少 " + ", ".join(missing))
            if unexpected:
                details.append("不允许 " + ", ".join(unexpected))
            raise ContractArgumentError(
                "canonical execute 输入只允许 plan_id、confirmation_id；" + "；".join(details))
    if operation == "approve" and "approved_by" in payload:
        raise ContractArgumentError(
            "canonical approve 不接受 approved_by；必须提交受信 ApprovalProof")
    if operation == "paper":
        payload = {**payload, "mode": "PAPER"}
    core = dbm.get_core_conn()
    execution = dbm.get_execution_conn() if operation in {"request-approval", "approve"} else None
    providers = []
    openalice = OpenAliceCommandProvider.from_env()
    if openalice is not None:
        providers.append(openalice)
    app = TradingCatApplication(
        core, execution, fundamental_provider=FundamentalProviderChain(providers),
        security_provider=LazyLongbridgeSecurityProvider(),
        seed_defaults=operation not in {"backtest", "status", "report"},
    )
    result = getattr(app, target)(**payload)
    if operation in {"analyze", "backtest", "propose", "paper", "status", "report"}:
        result["operation"] = envelope_operation
    return result


def main(argv=None) -> int:
    operation = argv[0] if argv else None
    try:
        args = _parse_args(argv)
        operation = args.operation or operation or "unknown"
        if args.operation not in OPERATIONS:
            result, exit_code = _error(
                operation, "UNKNOWN_OPERATION",
                f"不支持的 operation: {args.operation or '<missing>'}"), 2
        else:
            try:
                payload = _read_payload(args.input)
                result = _invoke(args.operation, payload)
                exit_code = 0 if result.get("ok") else 1
            except ContractArgumentError as exc:
                result, exit_code = _error(
                    OPERATIONS[args.operation][1], "ARGUMENT_ERROR", str(exc)), 2
            except (TypeError, ValueError) as exc:
                code = getattr(exc, "error_code", None)
                result, exit_code = _error(
                    OPERATIONS[args.operation][1], code or "INVALID_REQUEST", str(exc)), \
                    1 if code else 2
            except Exception as exc:
                code = getattr(exc, "error_code", None)
                if code:
                    result, exit_code = _error(
                        OPERATIONS[args.operation][1], code, str(exc),
                        retryable=bool(getattr(exc, "retryable", False))), 1
                else:
                    result, exit_code = _error(
                        OPERATIONS[args.operation][1], "INTERNAL_ERROR", str(exc),
                        retryable=bool(getattr(exc, "retryable", False))), 1
    except ContractArgumentError as exc:
        result, exit_code = _error(operation or "unknown", "ARGUMENT_ERROR", str(exc)), 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
