"""Fail closed on credentials accidentally committed to tracked files."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    names = {"DATACENTER_API_KEY", "FRED_API_KEY", "BINANCE_API_KEY", "YFINANCE_API_KEY"}
    values = {os.getenv(name, "") for name in names}
    values = {value for value in values if len(value) >= 8}
    files = subprocess.check_output(["git", "ls-files", "-z"], text=False).split(b"\0")
    findings = []
    for raw in files:
        if not raw:
            continue
        path = Path(raw.decode())
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if b"BEGIN PRIVATE KEY" in content or any(value.encode() in content for value in values):
            findings.append(str(path))
    if findings:
        print("secret scan failed: " + ", ".join(findings), file=sys.stderr)
        return 1
    print(f"secret scan passed ({len(files)} tracked files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
