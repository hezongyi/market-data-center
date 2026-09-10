"""Small, versioned operational receipts with safe environment metadata."""
from __future__ import annotations

import json
import os
import platform
import sqlite3
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
    deployment = _deployment_receipt_identity()
    return {
        "receipt_version": "operational-receipt.v1",
        "receipt_id": uuid4().hex,
        "action": action,
        "commit": deployment.get("source_commit") or source_commit(),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": sys.platform,
        },
        "command": command,
        "started_at": started_at,
        "completed_at": utc_now(),
        "software_version": deployment.get("software_version") or __version__,
        "deployment_id": deployment.get("deployment_id"),
        "result": result,
        "failure_stage": failure_stage,
        "error_category": error_category,
        "details": details or {},
    }


def _deployment_receipt_identity() -> dict:
    manifest = os.environ.get("DATACENTER_DEPLOYMENT_MANIFEST")
    if not manifest:
        return {}
    try:
        payload = json.loads(Path(manifest).read_text())
        return {
            key: payload[key]
            for key in ("deployment_id", "software_version", "source_commit")
            if isinstance(payload.get(key), str)
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def write_receipt(root: Path | None, receipt: dict) -> Path | None:
    if root is None:
        return None
    directory = Path(root) / "operations" / receipt["action"]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / f"{receipt['completed_at'].replace(':', '')}-{receipt['receipt_id']}.json"
    with target.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2, sort_keys=True)
        stream.write("\n")
    try:
        from data_center.snapshot import ReceiptIndex

        ReceiptIndex(Path(root)).add(receipt, str(target.relative_to(Path(root))))
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        _write_index_repair_need(Path(root), receipt, target, exc)
        return target
    return target


def _write_index_repair_need(root: Path, receipt: dict, target: Path, exc: Exception) -> None:
    """Record index degradation without changing the successful source receipt."""
    try:
        directory = root / "operations" / "receipt_index_repair_needed"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        repair = operation_receipt(
            action="receipt_index_repair_needed",
            command="data_center.evidence write_receipt",
            started_at=receipt["completed_at"],
            result="failed",
            failure_stage="index_update",
            error_category=type(exc).__name__,
            details={
                "source_receipt_id": receipt["receipt_id"],
                "source_receipt_reference": str(target.relative_to(root)),
            },
        )
        repair_target = directory / f"{repair['completed_at'].replace(':', '')}-{repair['receipt_id']}.json"
        with repair_target.open("x", encoding="utf-8") as stream:
            json.dump(repair, stream, indent=2, sort_keys=True)
            stream.write("\n")
    except (OSError, ValueError, KeyError, TypeError):
        pass
