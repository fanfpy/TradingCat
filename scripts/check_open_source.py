#!/usr/bin/env python3
"""Fail-closed checks for files intended for the public TradingCat repository.

The scanner never prints matched secret values. It checks the current release candidate,
not ignored local runtime files. Use Gitleaks on the final standalone repository to scan its
complete history before making it public.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "LICENSE", "NOTICE", "README.md", "README_EN.md", "CONTRIBUTING.md",
    "SECURITY.md", "DISCLAIMER.md", ".env.example", ".gitignore",
    "pyproject.toml", "requirements.txt", "requirements-dev.txt",
    "MANIFEST.in", "static/tradingcat-icon.svg",
    "SKILL.md", "agents/openai.yaml", "docs/architecture.md",
    "docs/agent-integration.md", "docs/open-source-release.md",
    "scripts/check_distribution.py", "scripts/build_source_archive.py",
    ".github/workflows/ci.yml", ".github/workflows/gitleaks.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/pull_request_template.md",
}
FORBIDDEN_PARTS = {"__pycache__", ".pytest_cache", ".venv", "reports", "data"}
FORBIDDEN_NAMES = {".env", "subscriptions.json"}
OLD_REFERENCES = (
    "architecture-v4.md", "architecture-v5.md", "todo-decision-chain.md",
    "todo-architecture-v5.md", "subscriptions.json",
)
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


def _candidate_files() -> list[Path]:
    try:
        output = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "."],
            cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return [path.relative_to(ROOT) for path in ROOT.rglob("*") if path.is_file()]
    return [Path(line) for line in output.splitlines()
            if line and (ROOT / line).is_file()]


def main() -> int:
    errors: list[str] = []
    candidates = _candidate_files()
    candidate_names = {path.as_posix() for path in candidates}

    missing = sorted(path for path in REQUIRED if not (ROOT / path).is_file())
    if missing:
        errors.append("missing required files: " + ", ".join(missing))

    for relative in candidates:
        parts = set(relative.parts)
        name = relative.name
        if parts & FORBIDDEN_PARTS or name in FORBIDDEN_NAMES:
            errors.append(f"runtime/private artifact would be published: {relative}")
        if name.endswith(".db") or ".db." in name or name.endswith((".pem", ".key")):
            errors.append(f"database/key artifact would be published: {relative}")

        path = ROOT / relative
        if not path.is_file() or path.stat().st_size > 5_000_000:
            continue
        data = path.read_bytes()
        if b"\x00" in data[:4096]:
            continue
        for rule, pattern in SECRET_RULES.items():
            if pattern.search(data):
                errors.append(f"possible {rule} in {relative}; value intentionally hidden")
        if relative.suffix.lower() in {".md", ".py", ".yaml", ".yml", ".toml"}:
            text = data.decode("utf-8", errors="replace")
            for old in OLD_REFERENCES:
                if old in text and relative.as_posix() != "scripts/check_open_source.py":
                    errors.append(f"obsolete reference {old} in {relative}")

    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if "longbridge==0.2.74" not in requirements or "longbridge==0.2.74" not in pyproject:
        errors.append("Longbridge must remain exactly pinned to 0.2.74")
    if "Apache License" not in (ROOT / "LICENSE").read_text(encoding="utf-8"):
        errors.append("LICENSE is not Apache License 2.0 text")

    # A standalone public repo must not accidentally omit these because of a broad ignore rule.
    for required in REQUIRED:
        if required not in candidate_names and (ROOT / required).exists():
            errors.append(f"required file is ignored by Git: {required}")

    if errors:
        print("open_source_check=FAIL", file=sys.stderr)
        for error in sorted(set(errors)):
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"open_source_check=PASS files_scanned={len(candidates)}")
    print("note=run gitleaks on the final standalone repository history before publication")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
