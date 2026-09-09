import time
from datetime import datetime, timezone
from pathlib import Path

from data_center.domain.models import IngestJob
from data_center.ingest.worker import LocalWorker
from data_center.runs.ledger import RunLedger


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


def test_worker_timeout_kills_child_and_prevents_late_write(tmp_path: Path, monkeypatch):
    import os
    import sys

    import pytest

    ledger = RunLedger(tmp_path / "ledger.sqlite")
    worker = LocalWorker(tmp_path / "lake", ledger, timeout_seconds=0.3)
    job = IngestJob(job_id="slow", symbol="BTC", start=datetime.now(timezone.utc), end=datetime.now(timezone.utc))
    run_id = worker.submit(job)
    code = "import os,time; from pathlib import Path; p=Path(__import__('sys').argv[1]); (p/'pid').write_text(str(os.getpid())); time.sleep(1); (p/'late').write_text('bad')"
    monkeypatch.setattr(worker, "_command", lambda directory: [sys.executable, "-c", code, str(directory)])
    started = time.monotonic()
    worker.run_next()
    assert time.monotonic() - started < 0.9
    directory = tmp_path / 'lake' / '.ingest-staging' / run_id / '1'
    pid = int((directory / 'pid').read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    time.sleep(1)
    assert not (directory / 'late').exists()
    assert ledger.get(run_id)["status"] == "queued"
    assert ledger.get(run_id)["error_type"] == "TimeoutError"
    assert ledger.get(run_id)["failure_stage"] == "supervise"
    assert ledger.claim_next_job() is None


def test_retry_keeps_original_receipt(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    run_id = ledger.enqueue_job({"job_id": "x", "dataset_id": "provider_bars"})
    claim = ledger.claim_next_job()
    ledger.fail_job(claim["job_id"], run_id, "invalid", retryable=False)
    original = ledger.get(run_id)
    replacement = ledger.retry_run(run_id)
    assert ledger.get(run_id) == original
    assert ledger.get(replacement)["retry_of"] == run_id
    assert ledger.get(replacement)["status"] == "queued"


def test_worker_construction_does_not_recover_running_jobs(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    run_id = ledger.enqueue_job({"job_id": "x", "dataset_id": "provider_bars"})
    ledger.claim_next_job()
    LocalWorker(tmp_path / "lake", ledger)
    assert ledger.get(run_id)["status"] == "running"


def test_publication_failure_recovers_without_refetch(tmp_path, monkeypatch):
    import pytest

    ledger = RunLedger(tmp_path / "ledger.sqlite")
    worker = LocalWorker(tmp_path / "lake", ledger)
    job = IngestJob(job_id="recover", symbol="TEST", start=datetime(2025, 12, 31, tzinfo=timezone.utc),
                    end=datetime(2026, 1, 1, tzinfo=timezone.utc))
    run_id = worker.submit(job)
    finish = ledger.finish_job
    monkeypatch.setattr(ledger, "finish_job", lambda *args: (_ for _ in ()).throw(OSError("ledger unavailable")))
    with pytest.raises(OSError):
        worker.run_next()
    paths = list((tmp_path / "lake" / "provider_bars").rglob("*.parquet"))
    assert len(paths) == 2
    original = {str(p): p.read_bytes() for p in paths}
    assert ledger.get(run_id)["status"] == "running"
    monkeypatch.setattr(ledger, "finish_job", finish)
    assert worker.run_next() is False
    assert ledger.get(run_id)["status"] == "pass"
    assert {str(p): p.read_bytes() for p in paths} == original
    assert not (tmp_path / "lake" / ".ingest-staging" / run_id / "2").exists()


def test_competing_worker_cannot_recover_or_claim(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    owner = LocalWorker(tmp_path / "lake", ledger)
    run_id = ledger.enqueue_job({"job_id": "x", "dataset_id": "provider_bars"})
    ledger.claim_next_job()
    with owner._ownership():
        assert LocalWorker(tmp_path / "lake", ledger).run_next() is False
    assert ledger.get(run_id)["status"] == "running"


def test_interrupted_attempt_is_delayed_then_retried(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    worker = LocalWorker(tmp_path / "lake", ledger)
    run_id = worker.submit(IngestJob(job_id="interrupted", symbol="TEST",
                                    start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                    end=datetime(2026, 1, 2, tzinfo=timezone.utc)))
    ledger.claim_next_job()
    assert worker.run_next() is False
    receipt = ledger.get(run_id)
    assert receipt["status"] == "queued"
    assert receipt["error_type"] == "WorkerInterrupted"
    assert receipt["next_attempt_at"] > time.time()
