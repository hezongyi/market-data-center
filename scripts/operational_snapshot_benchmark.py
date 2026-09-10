"""Retained acceptance benchmark for bounded operational snapshot hot paths."""
from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from data_center.observability import AlertSink, check_alerts, run_metrics
from data_center.runs.ledger import RunLedger
from data_center.snapshot import ReceiptIndex


def _commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _seed_runs(ledger: RunLedger, count: int) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(ledger.path) as database:
        database.executemany(
            "insert into runs(run_id,payload) values (?,?)",
            ((str(index), json.dumps({
                "run_id": str(index), "job_id": str(index), "dataset_id": "provider_bars",
                "provider": "fixture", "run_scope": "production", "status": "pass",
                "created_at": stamp, "started_at": stamp, "finished_at": stamp,
            })) for index in range(count)),
        )


def run_benchmark(receipt_count: int = 10_000, run_count: int = 10_000) -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix="mdc-snapshot-benchmark-") as directory:
        root = Path(directory)
        evidence = root / "evidence"
        receipts = evidence / "operations" / "backup"
        receipts.mkdir(parents=True)
        for index in range(receipt_count):
            payload = {
                "receipt_id": str(index), "action": "backup", "result": "pass",
                "completed_at": f"2026-09-10T00:{index // 60 % 60:02d}:{index % 60:02d}+00:00",
                "details": {"file_count": index},
            }
            (receipts / f"{index:05d}.json").write_text(json.dumps(payload))
        index = ReceiptIndex(evidence)
        rebuild = index.rebuild()

        ledger = RunLedger(root / "ledger.sqlite")
        _seed_runs(ledger, run_count)
        cold_started = time.monotonic()
        metrics = run_metrics(ledger, receipt_index=index, evidence_root=evidence)
        cold_seconds = time.monotonic() - cold_started

        shared = root / "shared-unrelated"
        shared.mkdir()
        for number in range(10_000):
            (shared / f"unrelated-{number}").touch()
        warm = []
        for _ in range(20):
            sample_started = time.monotonic()
            metrics = run_metrics(ledger, receipt_index=index, evidence_root=evidence)
            warm.append(time.monotonic() - sample_started)
        warm_p95 = sorted(warm)[18]

        evaluation_started = time.monotonic()
        check_alerts(ledger, AlertSink(root / "alerts"), metrics=metrics)
        evaluation_seconds = time.monotonic() - evaluation_started
        if cold_seconds >= 3.0 or warm_p95 >= 0.5 or evaluation_seconds >= 5.0:
            raise RuntimeError("operational snapshot performance threshold exceeded")
        return {
            "receipt_version": "operational-receipt.v1",
            "action": "operational_snapshot_benchmark",
            "commit": _commit(),
            "environment": {"python": platform.python_version(), "platform": platform.system()},
            "command": "python scripts/operational_snapshot_benchmark.py",
            "started_at": started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "result": "pass",
            "failure_stage": None,
            "error_category": None,
            "details": {
                "receipt_count": receipt_count,
                "run_count": run_count,
                "unrelated_file_count": 10_000,
                "receipt_index_rebuild_seconds": rebuild["duration_seconds"],
                "cold_metrics_seconds": cold_seconds,
                "warm_metrics_p95_seconds": warm_p95,
                "monitor_evaluation_seconds": evaluation_seconds,
                "thresholds": {"cold_seconds": 3.0, "warm_p95_seconds": 0.5,
                               "monitor_evaluation_seconds": 5.0},
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path,
                        default=Path("acceptance-receipts/operational-snapshot-benchmark.json"))
    args = parser.parse_args()
    report = run_benchmark()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": report["result"], **report["details"]}, sort_keys=True))


if __name__ == "__main__":
    main()
