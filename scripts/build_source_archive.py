#!/usr/bin/env python3
"""Build a deterministic, standalone TradingCat open-source archive."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import re
import subprocess
import sys
import tarfile
from pathlib import Path

from check_distribution import audit


ROOT = Path(__file__).resolve().parents[1]


def _candidate_files() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "."],
        cwd=ROOT,
        text=True,
    )
    return sorted(
        Path(line) for line in output.splitlines()
        if line and (ROOT / line).is_file()
    )


def _version() -> str:
    content = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', content)
    if not match:
        raise RuntimeError("project version not found in pyproject.toml")
    return match.group(1)


def build(output: Path) -> tuple[int, str]:
    files = _candidate_files()
    prefix = f"tradingcat-{_version()}"
    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for relative in files:
                    source = ROOT / relative
                    data = source.read_bytes()
                    info = tarfile.TarInfo(f"{prefix}/{relative.as_posix()}")
                    info.size = len(data)
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mode = 0o755 if source.stat().st_mode & 0o111 else 0o644
                    archive.addfile(info, io.BytesIO(data))

    errors = audit(output)
    if errors:
        output.unlink(missing_ok=True)
        raise RuntimeError("source archive audit failed: " + "; ".join(errors))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return len(files), digest


def main() -> int:
    parser = argparse.ArgumentParser(description="构建可公开发布的 TradingCat 独立源码包")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_open_source.py")],
        cwd=ROOT,
        check=True,
    )
    output = args.output or ROOT / "dist" / f"tradingcat-{_version()}-source.tar.gz"
    count, digest = build(output.resolve())
    print(f"source_archive=PASS file={output.resolve()}")
    print(f"files={count}")
    print(f"sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
