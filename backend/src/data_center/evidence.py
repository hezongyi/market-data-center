"""Small, versioned operational receipts with safe environment metadata."""
from __future__ import annotations

import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from data_center import __version__


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def operation_receipt(*, action: str, command: str, started_at: str, result: str,
                      failure_stage: str | None = None,
                      error_category: str | None = None, details: dict | None = None) -> dict:
    return {
        "receipt_version": "operational-receipt.v1",
        "receipt_id": uuid4().hex,
        "action": action,
        "commit": source_commit(),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": sys.platform,
        },
        "command": command,
        "started_at": started_at,
        "completed_at": utc_now(),
        "software_version": __version__,
        "result": result,
        "failure_stage": failure_stage,
        "error_category": error_category,
        "details": details or {},
    }


def write_receipt(root: Path | None, receipt: dict) -> Path | None:
    if root is None:
        return None
    directory = Path(root) / "operations" / receipt["action"]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / f"{receipt['completed_at'].replace(':', '')}-{receipt['receipt_id']}.json"
    with target.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return target
