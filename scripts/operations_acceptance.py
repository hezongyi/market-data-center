"""Run a bounded backup/recovery and fault-isolation drill without touching production data."""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from data_center.capacity import CapacityPolicy, CapacityProtectedError
from data_center.domain.models import IngestJob
from data_center.ingest.worker import LocalWorker
from data_center.observability import AlertSink, check_alerts, run_metrics
from data_center.operations import (
    create_backup,
    recovery_drill,
    retention_audit,
    verify_backup,
)
from data_center.runs.ledger import RunLedger
from data_center.snapshot import ReceiptIndex


def _commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def run_drill(output: Path | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="market-data-center-ops-") as directory:
        root = Path(directory) / "canonical"
        ledger = RunLedger(root / "audit" / "data_center.sqlite")
        worker = LocalWorker(root, ledger)
        run_id = worker.submit(IngestJob(job_id="operations-acceptance", symbol="TEST", run_scope="acceptance",
                                         start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                         end=datetime(2026, 1, 2, tzinfo=timezone.utc)))
        if not worker.run_next() or ledger.get(run_id)["status"] != "pass":
            raise RuntimeError("fixture run did not pass")
        evidence = Path(directory) / "evidence"
        backup = create_backup(root, ledger.path, Path(directory) / "backup.tar.gz", evidence_root=evidence)
        verification = verify_backup(Path(backup["archive"]), evidence_root=evidence)
        recovery = recovery_drill(root, ledger.path, Path(directory) / "recovery", evidence_root=evidence)
        capacity = retention_audit(root, capacity_policy=CapacityPolicy(), evidence_root=evidence)
        receipt_index = ReceiptIndex(evidence)
        metrics = run_metrics(
            ledger, canonical_root=root, capacity_policy=CapacityPolicy(),
            evidence_root=evidence, receipt_index=receipt_index,
            backup_root=Path(directory) / "backup-artifacts",
            restore_staging_root=Path(directory) / "restore-staging",
        )
        if (metrics["operational_snapshot_status"] != "fresh"
                or not metrics["last_successful_backup_at"]
                or not metrics["last_successful_recovery_drill_at"]):
            raise RuntimeError("operational snapshot did not expose indexed backup and recovery evidence")
        sink = AlertSink(Path(directory) / "alerts")
        event_ids = check_alerts(ledger, sink, heartbeat_limit=0, metrics=metrics)
        warning_policy = CapacityPolicy(warning_free_ratio=0.99, critical_free_ratio=0.01)
        critical_policy = CapacityPolicy(warning_free_ratio=0.999, critical_free_ratio=0.99)
        warning_policy.require_ingest_capacity(root)
        try:
            warning_policy.require_backfill_capacity(root, requested_days=32)
        except CapacityProtectedError:
            warning_backfill_blocked = True
        else:
            raise RuntimeError("warning policy did not block broad backfill")
        try:
            critical_policy.require_ingest_capacity(root)
        except CapacityProtectedError:
            critical_ingest_blocked = True
        else:
            raise RuntimeError("critical policy did not block ingest")
        capacity_sink = AlertSink(Path(directory) / "capacity-alerts")
        check_alerts(ledger, capacity_sink, canonical_root=root, capacity_policy=warning_policy)
        check_alerts(ledger, capacity_sink, canonical_root=root, capacity_policy=warning_policy)
        capacity_events = [event for event in capacity_sink.events()
                           if event["event"] == "capacity_warning"]
        if len(capacity_events) != 1:
            raise RuntimeError("capacity alert was not idempotent")
        report = {"status": "pass", "checked_at": datetime.now(timezone.utc).isoformat(),
                  "validation_command": "python scripts/operations_acceptance.py",
                  "environment": {"commit": _commit(), "python": platform.python_version(), "host": platform.node()},
                  "run_id": run_id, "backup": backup, "verification": verification,
                  "recovery": recovery, "fault_isolation": {"alert_event_count": len(event_ids),
                                                               "alert_event_ids": event_ids},
                  "capacity": capacity["capacity"],
                  "operational_snapshot": {
                      "status": metrics["operational_snapshot_status"],
                      "generation_seconds": metrics["snapshot_generation_seconds"],
                      "last_successful_backup_at": metrics["last_successful_backup_at"],
                      "last_successful_recovery_drill_at": metrics["last_successful_recovery_drill_at"],
                  },
                  "capacity_gates": {"warning_backfill_blocked": warning_backfill_blocked,
                                     "critical_ingest_blocked": critical_ingest_blocked,
                                     "idempotent_capacity_events": len(capacity_events)}}
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path,
                        default=Path("acceptance-receipts/operations-acceptance.json"))
    args = parser.parse_args()
    report = run_drill(args.output)
    print(json.dumps({"status": report["status"], "file_count": report["recovery"]["file_count"],
                      "alert_event_count": report["fault_isolation"]["alert_event_count"]}, sort_keys=True))


if __name__ == "__main__":
    main()
