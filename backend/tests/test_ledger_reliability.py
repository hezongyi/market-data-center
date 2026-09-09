from pathlib import Path

from data_center.runs.ledger import RunLedger
from data_center.ingest.worker import LocalWorker
from data_center.domain.models import IngestJob
from datetime import datetime, timezone
import time


def test_failed_jobs_retry_then_dead_letter(tmp_path: Path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    run_id = ledger.enqueue_job({"job_id": "j1", "dataset_id": "x"})
    for attempt in range(3):
        claimed = ledger.claim_next_job()
        assert claimed is not None
        ledger.fail_job(claimed["job_id"], run_id, f"error-{attempt}")
    assert ledger.get(run_id)["status"] == "dead_letter"
    assert ledger.claim_next_job() is None


def test_heartbeat_age_is_recorded(tmp_path: Path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    assert ledger.heartbeat_age_seconds() is None
    ledger.heartbeat()
    assert ledger.heartbeat_age_seconds() < 5


def test_worker_timeout_is_retryable(tmp_path: Path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    worker = LocalWorker(tmp_path / "lake", ledger, timeout_seconds=0.001)
    job = IngestJob(job_id="slow", symbol="BTC", start=datetime.now(timezone.utc), end=datetime.now(timezone.utc))
    original = worker.ledger
    run_id = worker.submit(job)
    # A tiny timeout still exercises the configured boundary deterministically
    # when the connector is replaced with a slow callable.
    import data_center.ingest.worker as worker_module
    old = worker_module.run_fixture_ingest
    worker_module.run_fixture_ingest = lambda *args, **kwargs: time.sleep(0.05)
    try:
        worker.run_next()
    finally:
        worker_module.run_fixture_ingest = old
    assert original.get(run_id)["status"] == "queued"
