import json
import sqlite3
import time

from data_center.ingest.worker import LocalWorker
from data_center.observability import AlertSink
from data_center.runs.ledger import RunLedger
from data_center.settings import Settings


def main() -> None:
    settings = Settings()
    from data_center.deployment import validated_runtime_identity
    identity = validated_runtime_identity(
        settings.deployment_manifest, settings.evidence_root, component="worker",
    ) if settings.deployment_manifest else {
        "deployment_id": "development", "software_version": "unknown", "source_commit": "unknown"
    }
    print(json.dumps({"event": "worker_started", "request_id": None, **{k: identity[k] for k in ("deployment_id", "software_version", "source_commit")}}), flush=True)
    worker = LocalWorker(settings.canonical_root, RunLedger(settings.ledger_path), timeout_seconds=settings.worker_timeout_seconds,
                         alert_sink=AlertSink(settings.evidence_root / "alerts", settings.alerts_enabled),
                         capacity_policy=settings.capacity_policy())
    while True:
        try:
            worked = worker.run_next()
        except sqlite3.OperationalError as exc:
            # API, scheduler and worker intentionally share one WAL ledger.  A
            # write can still lose the busy-timeout race under a large
            # reconciliation transaction; that is transient contention, not a
            # reason to abandon the durable queue and strand its jobs.
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            print(json.dumps({"event": "worker_ledger_busy", "request_id": None,
                              "retry_seconds": 1.0}), flush=True)
            time.sleep(1)
            continue
        if not worked:
            time.sleep(1)


if __name__ == "__main__":
    main()
