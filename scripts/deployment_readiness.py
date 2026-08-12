#!/usr/bin/env python3
"""P0-A deployment-isolation readiness checks.

This module deliberately has no broker client.  It only inspects local service
configuration, filesystem metadata, and the executiond ``health`` operation.
Credential values are never returned, logged, or hashed.
"""

import json
import os
import platform
import socket
import stat
import subprocess
import argparse
from pathlib import Path

try:  # These POSIX modules are intentionally unavailable on Windows.
    import grp
    import pwd
except ImportError:  # pragma: no cover - exercised by the Windows NOT_RUN path
    grp = None
    pwd = None


REQUIRED_SANDBOX = {
    "NoNewPrivileges": "yes",
    "PrivateTmp": "yes",
    "ProtectSystem": "strict",
    "ProtectHome": "yes",
    "UMask": "0077",
}
TRADE_KEYS = {
    "LONGBRIDGE_TRADE_APP_KEY",
    "LONGBRIDGE_TRADE_APP_SECRET",
    "LONGBRIDGE_TRADE_ACCESS_TOKEN",
}


def _result(status, reason, **details):
    return {"status": status, "reason": reason, **details}


def _systemctl(args):
    try:
        proc = subprocess.run(
            ["systemctl", *args], text=True, capture_output=True, check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if proc.returncode:
        return None, (proc.stderr or proc.stdout).strip()
    return proc.stdout, ""


def _env_keys(path):
    """Return only assignment names from an EnvironmentFile, never values."""
    keys = set()
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].removeprefix("export ").strip())
    return keys


def _account(name):
    if pwd is None:
        return None
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def _group(name):
    if grp is None:
        return None
    try:
        return grp.getgrnam(name)
    except KeyError:
        return None


def _user_in_group(user, group_name):
    account = _account(user)
    group = _group(group_name)
    return bool(account and group and (account.pw_gid == group.gr_gid or user in group.gr_mem))


def _check_template(template_path):
    path = Path(template_path)
    if not path.is_file():
        return _result("FAIL", "executiond systemd template is missing", path=str(path))
    text = path.read_text(encoding="utf-8")
    required_lines = {
        "User=tradingcat-exec", "EnvironmentFile=/etc/tradingcat/executiond.env",
        "ProtectSystem=strict", "ProtectHome=true", "NoNewPrivileges=true",
        "PrivateTmp=true", "UMask=0077", "RuntimeDirectory=tradingcat",
        "SupplementaryGroups=tradingcat-core-read tradingcat-exec-client",
        "--socket-group tradingcat-exec-client",
    }
    missing = sorted(line for line in required_lines if line not in text)
    if missing:
        return _result("FAIL", "template misses required P0-A controls", missing=missing)
    return _result("PASS", "executiond template declares P0-A sandbox controls")


def _check_store(path, expected_uid, expected_gid, *, execution_store):
    target = Path(path)
    if not target.exists():
        return _result("FAIL", "store does not exist; cannot inspect ownership", path=str(target))
    info = target.stat()
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != expected_uid or info.st_gid != expected_gid:
        return _result("FAIL", "store owner/group does not match deployment policy",
                       path=str(target), mode=oct(mode), uid=info.st_uid, gid=info.st_gid)
    if execution_store:
        acceptable = mode == 0o600
        reason = "execution store is private to executiond"
    else:
        acceptable = mode == 0o640
        reason = "core store grants executiond read-only group access"
    if not acceptable:
        return _result("FAIL", "store mode is not least-privilege", path=str(target),
                       mode=oct(mode), expected="0600" if execution_store else "0640")
    return _result("PASS", reason, path=str(target), mode=oct(mode))


def _check_socket(path, execution_uid, socket_gid):
    target = Path(path)
    if not target.exists():
        return _result("FAIL", "executiond socket is absent", path=str(target))
    info = target.stat()
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISSOCK(info.st_mode):
        return _result("FAIL", "configured executiond path is not a Unix socket", path=str(target))
    if info.st_uid != execution_uid or info.st_gid != socket_gid or mode != 0o660:
        return _result("FAIL", "socket owner/group/mode is not P0-A compliant",
                       path=str(target), mode=oct(mode), uid=info.st_uid, gid=info.st_gid,
                       expected_mode="0660")
    return _result("PASS", "socket is executiond-owned and group-limited", path=str(target), mode=oct(mode))


def _check_execution_env_file(path, execution_gid):
    target = Path(path)
    if not target.is_file():
        return _result("FAIL", "executiond environment file is missing", path=str(target))
    info = target.stat()
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != 0 or info.st_gid != execution_gid or mode != 0o640:
        return _result("FAIL", "executiond credential file is not root-owned and executiond-group-limited",
                       path=str(target), mode=oct(mode), uid=info.st_uid, gid=info.st_gid,
                       expected_mode="0640")
    return _result("PASS", "executiond credential file is root-owned and executiond-group-limited",
                   path=str(target), mode=oct(mode))


def _check_health(path):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(path)
            client.sendall(b'{"operation":"health"}\n')
            raw = client.recv(4096)
        response = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _result("FAIL", "executiond health probe failed", error=type(exc).__name__)
    data = response.get("data", {})
    if not response.get("ok") or data.get("status") != "P0_A_DRY_RUN_ONLY" or data.get("live_submit_rpc") is not False:
        return _result("FAIL", "executiond health response violates dry-run boundary")
    return _result("PASS", "executiond health confirms P0-A dry-run-only RPC surface")


def _check_systemd(service, execution_user, execution_env_file):
    fields = ["User", "Group", "NoNewPrivileges", "PrivateTmp", "ProtectSystem",
              "ProtectHome", "UMask", "ReadOnlyPaths", "ReadWritePaths", "EnvironmentFiles",
              "SupplementaryGroups", "ExecStart", "ActiveState"]
    output, error = _systemctl(["show", service, "--no-pager", *[f"--property={x}" for x in fields]])
    if output is None:
        return _result("FAIL", "cannot inspect active systemd service", error=error)
    values = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    errors = []
    if values.get("User") != execution_user:
        errors.append("User")
    if values.get("Group") != execution_user:
        errors.append("Group")
    for key, expected in REQUIRED_SANDBOX.items():
        if values.get(key, "").lower() != expected:
            errors.append(key)
    if "/var/lib/tradingcat/core" not in values.get("ReadOnlyPaths", ""):
        errors.append("ReadOnlyPaths")
    if "/var/lib/tradingcat/execution" not in values.get("ReadWritePaths", ""):
        errors.append("ReadWritePaths")
    if str(execution_env_file) not in values.get("EnvironmentFiles", ""):
        errors.append("EnvironmentFiles")
    groups = values.get("SupplementaryGroups", "")
    if "tradingcat-exec-client" not in groups or "tradingcat-core-read" not in groups:
        errors.append("SupplementaryGroups")
    if "--socket-group tradingcat-exec-client" not in values.get("ExecStart", ""):
        errors.append("ExecStart(socket-group)")
    if values.get("ActiveState") != "active":
        errors.append("ActiveState")
    if errors:
        return _result("FAIL", "active systemd unit misses required P0-A controls", missing=sorted(errors))
    return _result("PASS", "active systemd unit has required P0-A sandbox controls")


def run_deployment_readiness(*, core_user, execution_user, execution_read_group,
                             socket_client_group, core_db, execution_db, socket_path,
                             core_env_file, execution_env_file, service,
                             template_path):
    """Inspect a deployed P0-A boundary without contacting a broker or placing orders."""
    checks = {"template": _check_template(template_path)}
    if platform.system() != "Linux":
        checks["field_environment"] = _result(
            "NOT_RUN", "P0-A field checks require a Linux host booted with systemd",
            platform=platform.system(),
        )
        return {"status": "NOT_RUN", "checks": checks,
                "reason": "Linux/systemd field environment is unavailable"}

    probe, probe_error = _systemctl(["is-system-running", "--wait"])
    if probe is None:
        checks["field_environment"] = _result("NOT_RUN", "systemd is not available", error=probe_error)
        return {"status": "NOT_RUN", "checks": checks,
                "reason": "Linux/systemd field environment is unavailable"}
    checks["field_environment"] = _result("PASS", "Linux systemd field environment detected")

    core = _account(core_user)
    execution = _account(execution_user)
    read_group = _group(execution_read_group)
    client_group = _group(socket_client_group)
    if not all((core, execution, read_group, client_group)):
        checks["accounts"] = _result("FAIL", "required users or groups do not exist")
        return {"status": "FAIL", "checks": checks, "reason": "required users/groups missing"}
    if core.pw_uid == execution.pw_uid:
        checks["accounts"] = _result("FAIL", "core and executiond must use distinct OS users")
    elif _user_in_group(core_user, execution_user):
        checks["accounts"] = _result("FAIL", "core user must not belong to executiond private group")
    elif not _user_in_group(execution_user, execution_read_group):
        checks["accounts"] = _result("FAIL", "executiond user lacks core read-only group")
    elif not _user_in_group(core_user, socket_client_group):
        checks["accounts"] = _result("FAIL", "core user lacks executiond socket group")
    else:
        checks["accounts"] = _result("PASS", "Core/executiond users and required group memberships are isolated")

    checks["stores_distinct"] = (
        _result("PASS", "core and execution stores use distinct canonical paths")
        if os.path.realpath(core_db) != os.path.realpath(execution_db)
        else _result("FAIL", "core and execution stores resolve to the same path")
    )
    checks["core_store_permissions"] = _check_store(
        core_db, core.pw_uid, read_group.gr_gid, execution_store=False)
    checks["execution_store_permissions"] = _check_store(
        execution_db, execution.pw_uid, execution.pw_gid, execution_store=True)
    checks["executiond_env_permissions"] = _check_execution_env_file(
        execution_env_file, execution.pw_gid)

    try:
        core_keys = _env_keys(core_env_file)
        execution_keys = _env_keys(execution_env_file)
        missing_trade = sorted(TRADE_KEYS - execution_keys)
        leaked_trade = sorted(TRADE_KEYS & core_keys)
        separate = "TRADINGCAT_REQUIRE_SEPARATE_CREDENTIALS" in execution_keys
        if missing_trade or leaked_trade or not separate:
            checks["credential_boundary"] = _result(
                "FAIL", "credential variable boundary is invalid", missing_trade_keys=missing_trade,
                core_trade_keys=leaked_trade, separate_credentials_enforced=separate)
        else:
            checks["credential_boundary"] = _result(
                "PASS", "trade credential names appear only in executiond environment file")
    except OSError as exc:
        checks["credential_boundary"] = _result("FAIL", "cannot read declared environment files",
                                                  error=type(exc).__name__)
    checks["systemd_sandbox"] = _check_systemd(service, execution_user, execution_env_file)
    checks["socket_permissions"] = _check_socket(socket_path, execution.pw_uid, client_group.gr_gid)
    checks["executiond_health"] = _check_health(socket_path)

    statuses = [item["status"] for item in checks.values()]
    status = "PASS" if all(item == "PASS" for item in statuses) else "FAIL"
    return {"status": status, "checks": checks,
            "reason": "all P0-A deployment isolation checks passed" if status == "PASS"
            else "one or more P0-A deployment isolation checks failed"}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Inspect P0-A deployment isolation; never contacts a broker or submits orders.")
    parser.add_argument("--core-user", default="tradingcat-core")
    parser.add_argument("--execution-user", default="tradingcat-exec")
    parser.add_argument("--execution-read-group", default="tradingcat-core-read")
    parser.add_argument("--socket-client-group", default="tradingcat-exec-client")
    parser.add_argument("--core-db", default=os.environ.get("TRADING_CORE_DB", "/var/lib/tradingcat/core/core.db"))
    parser.add_argument("--execution-db", default=os.environ.get("TRADING_EXECUTION_DB", "/var/lib/tradingcat/execution/execution.db"))
    parser.add_argument("--socket", default="/run/tradingcat/executiond.sock")
    parser.add_argument("--core-env-file", default="/etc/tradingcat/core.env")
    parser.add_argument("--execution-env-file", default="/etc/tradingcat/executiond.env")
    parser.add_argument("--service", default="tradingcat-executiond.service")
    parser.add_argument("--template", default=str(Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "tradingcat-executiond.service"))
    args = parser.parse_args(argv)
    values = vars(args)
    template_path = values.pop("template")
    values["socket_path"] = values.pop("socket")
    report = run_deployment_readiness(**values, template_path=template_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
