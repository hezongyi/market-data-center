"""Run a bounded backup/recovery and fault-isolation drill without touching production data."""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from data_center.domain.models import IngestJob
from data_center.ingest.worker import LocalWorker
from data_center.observability import AlertSink, check_alerts
from data_center.operations import create_backup, recovery_drill
from data_center.runs.ledger import RunLedger


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
        run_id = worker.submit(IngestJob(job_id="operations-acceptance", symbol="TEST",
                                         start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                         end=datetime(2026, 1, 2, tzinfo=timezone.utc)))
        if not worker.run_next() or ledger.get(run_id)["status"] != "pass":
            raise RuntimeError("fixture run did not pass")
        backup = create_backup(root, ledger.path, Path(directory) / "backup.tar.gz")
        recovery = recovery_drill(root, ledger.path, Path(directory) / "recovery")
        sink = AlertSink(Path(directory) / "alerts")
        event_ids = check_alerts(ledger, sink, heartbeat_limit=0)
        report = {"status": "pass", "checked_at": datetime.now(timezone.utc).isoformat(),
                  "validation_command": "python scripts/operations_acceptance.py",
                  "environment": {"commit": _commit(), "python": platform.python_version(), "host": platform.node()},
                  "run_id": run_id, "backup": backup,
                  "recovery": recovery, "fault_isolation": {"alert_event_count": len(event_ids),
                                                               "alert_event_ids": event_ids}}
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_drill(args.output)
    print(json.dumps({"status": report["status"], "file_count": report["recovery"]["file_count"],
                      "alert_event_count": report["fault_isolation"]["alert_event_count"]}, sort_keys=True))


if __name__ == "__main__":
    main()
