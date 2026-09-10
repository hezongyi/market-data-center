import json
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
        if not worker.run_next():
            time.sleep(1)


if __name__ == "__main__":
    main()
