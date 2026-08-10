#!/usr/bin/env python3
"""Audit built TradingCat distributions without printing possible secret values."""

from __future__ import annotations

import argparse
import re
import tarfile
import zipfile
from pathlib import Path


SECRET_RULES = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "github_token": re.compile(rb"gh[pousr]_[A-Za-z0-9_]{30,}"),
    "openai_key": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}"),
    "aws_access_key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "slack_token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}"),
    "longbridge_credential": re.compile(
        rb"(?m)^LONGBRIDGE_(?:APP_KEY|APP_SECRET|ACCESS_TOKEN)="
        rb"(?!your_|\.\.\.|\$\{|$).+"
    ),
}


def _contents(path: Path) -> list[tuple[str, bytes]]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return [(name, archive.read(name)) for name in archive.namelist()]
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            return [
                (member.name, archive.extractfile(member).read())
                for member in archive.getmembers()
                if member.isfile()
            ]
    raise ValueError(f"unsupported distribution format: {path.name}")


def audit(path: Path) -> list[str]:
    errors: list[str] = []
    contents = _contents(path)
    names = [name for name, _ in contents]
    lowered = [name.lower() for name in names]

    if any(name == ".env" or name.endswith("/.env") for name in names):
        errors.append("contains a runtime .env file")
    if any(name.endswith((".db", ".pem", ".key")) for name in lowered):
        errors.append("contains a database or private key file")
    if any("/reports/" in name or "/data/" in name for name in lowered):
        errors.append("contains runtime reports or data")
    if not any(name.endswith("/LICENSE") or "/licenses/LICENSE" in name
               for name in names):
        errors.append("does not contain LICENSE")
    if not any(name.endswith("/NOTICE") or "/licenses/NOTICE" in name
               for name in names):
        errors.append("does not contain NOTICE")

    for name, data in contents:
        for rule, pattern in SECRET_RULES.items():
            if pattern.search(data):
                errors.append(f"possible {rule} in {name}; value intentionally hidden")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="审计 TradingCat Python 发布制品")
    parser.add_argument("directory", nargs="?", default="dist")
    args = parser.parse_args()
    artifacts = sorted(Path(args.directory).glob("tradingcat_core-*"))
    if not artifacts:
        print("distribution_check=FAIL no artifacts found")
        return 1

    failed = False
    for artifact in artifacts:
        errors = audit(artifact)
        if errors:
            failed = True
            print(f"distribution_check=FAIL file={artifact.name}")
            for error in errors:
                print(f"- {error}")
        else:
            print(f"distribution_check=PASS file={artifact.name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
