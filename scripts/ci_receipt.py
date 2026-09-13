"""Write a structured receipt for a unified CI invocation."""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

WEB_PACKAGE = Path(__file__).resolve().parents[1] / "webui" / "package.json"


def software_version() -> str:
    """Read the release version from its single source instead of copying it."""
    try:
        return json.loads(WEB_PACKAGE.read_text())["version"]
    except (OSError, KeyError, json.JSONDecodeError):
        return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", choices=["pass", "failed"], required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--action", default="ci")
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    payload = {
        "receipt_version": "operational-receipt.v1",
        "action": args.action,
        "commit": commit,
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "command": f"bash scripts/ci.sh {args.scope}",
        "started_at": args.started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "software_version": software_version(),
        "result": args.result,
        "failure_stage": None if args.result == "pass" else args.scope,
        "error_category": None if args.result == "pass" else "CommandFailed",
        "details": {"scope": args.scope},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
