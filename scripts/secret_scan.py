"""Fail closed on credentials accidentally committed to tracked files."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_CREDENTIAL_NAMES = rb"(?:DATACENTER_API_KEY|FRED_API_KEY|BINANCE_API_KEY|YFINANCE_API_KEY)"
# Keep this line-oriented.  A source lookup such as `KEY="` followed by a
# later string literal is not a credential assignment.
_ASSIGNMENT = re.compile(
    rb"(?m)(?:^|[^A-Za-z0-9_])"
    + _CREDENTIAL_NAMES
    + rb"[ \t]*[:=][ \t]*['\"]([A-Za-z0-9][A-Za-z0-9._+/=-]{7,})['\"]"
)
_TOKEN = re.compile(
    rb"(?:ghp" + rb"_" + rb"|github" + rb"_" + rb"pat" + rb"_|sk-|xoxb-)"
    rb"[A-Za-z0-9_-]{12,}"
)


def has_secret(content: bytes) -> bool:
    private_key = b"-----" + b"BEGIN " + b"PRIVATE KEY-----"
    return private_key in content or bool(_ASSIGNMENT.search(content)) or bool(_TOKEN.search(content))


def main() -> int:
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
        if has_secret(content):
            findings.append(str(path))
    if findings:
        print("secret scan failed: " + ", ".join(findings), file=sys.stderr)
        return 1
    print(f"secret scan passed ({len(files)} tracked files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
