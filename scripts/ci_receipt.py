"""Write a structured receipt for a unified CI invocation."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from code_identity import checkout_identity

WEB_PACKAGE = Path(__file__).resolve().parents[1] / "webui" / "package.json"


def environment_identity(repo: Path) -> dict:
    packages = sorted((dist.metadata["Name"], dist.version) for dist in importlib.metadata.distributions())
    # Tools may consume arbitrary variables (PYTEST_ADDOPTS, NODE_OPTIONS,
    # VITE_*, proxies, etc.). Prefer cache misses over falsely matching evidence.
    relevant = dict(os.environ)
    for key in ("DATACENTER_CI_RECEIPT", "DATACENTER_CI_ACTION", "_", "SHLVL", "PWD", "OLDPWD"):
        relevant.pop(key, None)
    installed = repo / "webui/node_modules/.package-lock.json"
    try:
        node = subprocess.check_output(["node", "--version"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        node = "unavailable"
    return {"python": platform.python_version(), "python_executable": sys.executable,
            "platform": platform.platform(), "node": node,
            "python_packages_hash": hashlib.sha256(json.dumps(packages).encode()).hexdigest(),
            "web_install_hash": hashlib.sha256(installed.read_bytes()).hexdigest() if installed.exists() else None,
            "configuration_hash": hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()}


def reusable(receipt: dict, source: dict, environment: dict, request: dict) -> bool:
    if not isinstance(receipt, dict) or not isinstance(receipt.get("details"), dict):
        return False
    details = receipt["details"]
    checks = details.get("checks", [])
    return (receipt.get("result") == "pass" and receipt.get("source") == source
            and receipt.get("source_after") == source and receipt.get("environment") == environment
            and details.get("identity_stable") is True and details.get("request") == request
            and isinstance(checks, list) and bool(checks) and len(checks) == len(request["checks"])
            and all(isinstance(check, dict) and check.get("result") == "pass"
                    and check.get("exit_code") == 0 and check.get("name") == expected["name"]
                    and check.get("command") == expected["command"]
                    for check, expected in zip(checks, request["checks"])))


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
    source = checkout_identity(WEB_PACKAGE.parents[1])
    payload = {
        "receipt_version": "operational-receipt.v1",
        "action": args.action,
        "commit": source["commit"],
        "source": source,
        "environment": environment_identity(WEB_PACKAGE.parents[1]),
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
