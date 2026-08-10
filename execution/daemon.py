#!/usr/bin/env python3
"""P0-A executiond：Unix socket 服务，只持有 execution store 与审批密钥。

P0-A 故意不暴露真实下单 RPC；P0-B 小额验收前只允许创建/验证审批票据。
"""

import argparse
import json
import os
import socketserver
from pathlib import Path

from execution.approval_wechat import HMACIdentityVerifier, IdentityProof
from execution.service import ExecutionService
from shared import db as dbm


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            request = json.loads(self.rfile.readline().decode("utf-8"))
            result = self.server.dispatch(request)
            response = {"ok": True, "data": result}
        except Exception as exc:
            response = {"ok": False, "error": {"type": type(exc).__name__,
                                                 "message": str(exc)}}
        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


class ExecutionDaemon(socketserver.UnixStreamServer):
    allow_reuse_address = True

    def __init__(self, socket_path: str, service: ExecutionService):
        self.service = service
        super().__init__(socket_path, _Handler)

    def dispatch(self, request):
        operation = request.get("operation")
        if operation == "request_confirmation":
            return self.service.request_confirmation(
                request["plan_id"], confirmation_id=request.get("confirmation_id")).to_dict()
        if operation in ("approve", "reject"):
            raw = request["approval_proof"]
            proof = IdentityProof(
                subject=raw["subject"], timestamp=int(raw["timestamp"]),
                nonce=raw["nonce"], signature=raw["signature"])
            if operation == "approve":
                return self.service.approve(request["confirmation_id"], proof).to_dict()
            return self.service.reject(
                request["confirmation_id"], proof, request.get("reason", "")).to_dict()
        if operation == "health":
            return {"status": "P0_A_DRY_RUN_ONLY", "live_submit_rpc": False}
        raise ValueError(f"unsupported operation: {operation}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tradingcat-executiond")
    parser.add_argument("--socket", default="/run/tradingcat/executiond.sock")
    parser.add_argument("--core-db", default=dbm.CORE_DB_PATH)
    parser.add_argument("--execution-db", default=dbm.EXECUTION_DB_PATH)
    args = parser.parse_args(argv)

    socket_path = Path(args.socket)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()
    core = dbm.get_readonly_conn(args.core_db)
    execution = dbm.get_execution_conn(args.execution_db)
    verifier = HMACIdentityVerifier.from_env()
    service = ExecutionService(core, execution, identity_verifier=verifier)
    server = ExecutionDaemon(str(socket_path), service)
    os.chmod(socket_path, 0o660)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if socket_path.exists():
            socket_path.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
