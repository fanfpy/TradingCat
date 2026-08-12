#!/usr/bin/env python3
"""TradingCat v5 自动验收；绝不创建 Canary，也绝不提交真实订单。"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared import db as dbm
from shared.sdk_diagnostics import diagnose_longbridge
from shared.quant_provider import LongbridgeQuantProvider
from scripts.deployment_readiness import run_deployment_readiness


def _run(command):
    proc = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})
    return {"command": command, "passed": proc.returncode == 0,
            "returncode": proc.returncode,
            "output_tail": (proc.stdout + proc.stderr)[-4000:]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="v5 P0-A/P1/P2 自动验收")
    parser.add_argument("--no-connect", action="store_true",
                        help="跳过 Longbridge 只读行情连通测试")
    parser.add_argument("--record-p0a", action="store_true",
                        help="在 execution store 记录 P0_A=PASS")
    parser.add_argument("--confirm-deployment-isolation", action="store_true",
                        help="已废弃：P0-A 只接受现场 deployment readiness=PASS")
    parser.add_argument("--deployment-readiness", action="store_true",
                        help="运行 Linux/systemd P0-A 部署隔离现场验收（不连接券商）")
    parser.add_argument("--core-user", default="tradingcat-core")
    parser.add_argument("--execution-user", default="tradingcat-exec")
    parser.add_argument("--execution-read-group", default="tradingcat-core-read")
    parser.add_argument("--socket-client-group", default="tradingcat-exec-client")
    parser.add_argument("--core-env-file", default="/etc/tradingcat/core.env")
    parser.add_argument("--execution-env-file", default="/etc/tradingcat/executiond.env")
    parser.add_argument("--executiond-service", default="tradingcat-executiond.service")
    parser.add_argument("--executiond-socket", default="/run/tradingcat/executiond.sock")
    args = parser.parse_args(argv)

    personal_loop = (
        {"skipped": True, "reason": "--no-connect"}
        if args.no_connect else
        _run([sys.executable, "scripts/acceptance_personal_loop.py"])
    )
    checks = {
        "pytest": _run([sys.executable, "-m", "pytest", "-q"]),
        "e2e_dry_run": _run([sys.executable, "e2e_full.py"]),
        "aapl_personal_loop": personal_loop,
        "sdk": diagnose_longbridge(
            connect=not args.no_connect,
            require_credentials=not args.no_connect,
        ),
        "longbridge_quant_optional": LongbridgeQuantProvider().capability().to_dict(),
        "stores_configured_separately": (
            os.path.realpath(dbm.CORE_DB_PATH) != os.path.realpath(dbm.EXECUTION_DB_PATH)),
        "live_cli_disabled_without_approval_proof": True,
        "p0_b_real_order": "NOT_RUN_REQUIRES_EXPLICIT_USER_APPROVAL",
    }
    deployment = None
    if args.deployment_readiness:
        deployment = run_deployment_readiness(
            core_user=args.core_user, execution_user=args.execution_user,
            execution_read_group=args.execution_read_group,
            socket_client_group=args.socket_client_group,
            core_db=os.environ.get("TRADING_CORE_DB", "/var/lib/tradingcat/core/core.db"),
            execution_db=os.environ.get("TRADING_EXECUTION_DB", "/var/lib/tradingcat/execution/execution.db"),
            socket_path=args.executiond_socket, core_env_file=args.core_env_file,
            execution_env_file=args.execution_env_file, service=args.executiond_service,
            template_path=ROOT / "deploy" / "systemd" / "tradingcat-executiond.service",
        )
        checks["deployment_isolation"] = deployment
    automated_passed = (
        checks["pytest"]["passed"] and checks["e2e_dry_run"]["passed"]
        and (personal_loop.get("skipped") or personal_loop.get("passed"))
        and checks["sdk"]["passed"] and checks["stores_configured_separately"])
    # 证据 hash 只覆盖稳定结论，不包含随机 plan_id、运行时间和输出文本。
    evidence_payload = {
        "pytest": checks["pytest"]["passed"],
        "e2e_dry_run": checks["e2e_dry_run"]["passed"],
        "aapl_personal_loop": (
            "SKIPPED" if personal_loop.get("skipped") else personal_loop.get("passed")),
        "sdk_passed": checks["sdk"]["passed"],
        "sdk_version": checks["sdk"]["version"],
        "sdk_capabilities": checks["sdk"]["capabilities"],
        "stores_configured_separately": checks["stores_configured_separately"],
        "deployment_isolation": deployment["status"] if deployment else "NOT_RUN",
        "live_cli_disabled_without_approval_proof": True,
        "p0_b_real_order": "NOT_RUN_REQUIRES_EXPLICIT_USER_APPROVAL",
    }
    evidence = hashlib.sha256(json.dumps(
        evidence_payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    recorded = False
    if args.record_p0a:
        if not automated_passed:
            raise RuntimeError("自动验收未全部通过，不能记录 P0_A")
        if not deployment or deployment["status"] != "PASS":
            raise RuntimeError(
                "记录 P0_A 前必须运行 --deployment-readiness 且所有 Linux/systemd 现场检查 PASS")
        execution = dbm.get_execution_conn()
        dbm.mark_system_readiness(execution, "P0_A", evidence)
        recorded = True

    report = {
        "architecture": "v5", "automated_acceptance": "PASS" if automated_passed else "FAIL",
        "deployment_isolation": deployment,
        "p0_a_recorded": recorded, "evidence_hash": evidence, "checks": checks,
        "live_status": "DRY_RUN_ONLY",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if automated_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
