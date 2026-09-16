import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from data_center.catalog.manifest import manifest_path
from data_center.dataset_center import (
    DatasetCenter,
    DatasetMember,
    managed_dataset_root,
)
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

def test_paused_maintenance_task_is_not_claimed(tmp_path: Path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    run_id = ledger.enqueue_job({"job_id": "paused-task", "dataset_id": "provider_bars", "run_scope": "maintenance"})
    ledger.upsert_maintenance_task("paused-task", {"task_id": "paused-task", "run_ids": [run_id]}, "paused")
    assert ledger.claim_next_job() is None
    ledger.update_maintenance_task_status("paused-task", "enabled")
    assert ledger.claim_next_job() is not None


@pytest.mark.parametrize("run_scope", ["maintenance", "production"])
def test_provider_coverage_gap_does_not_consume_the_worker_retry_budget(run_scope):
    from data_center.ingest.process import safe_failure_result
    from data_center.quality.errors import QualityError

    result = safe_failure_result(
        QualityError("coverage", [{"code": "coverage_not_ready"}]),
        {"run_scope": run_scope},
    )
    assert result["retryable"] is False


def test_structural_quality_failure_is_not_retryable_for_maintenance():
    from data_center.ingest.process import safe_failure_result
    from data_center.quality.errors import QualityError

    result = safe_failure_result(
        QualityError("schema", [{"code": "duplicate_timestamp"}]),
        {"run_scope": "maintenance"},
    )
    assert result["retryable"] is False


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


def test_run_scope_is_immutable_before_and_after_terminal_state(tmp_path):
    import pytest

    ledger = RunLedger(tmp_path / "ledger.sqlite")
    run_id = ledger.enqueue_job({"job_id": "x", "dataset_id": "provider_bars", "run_scope": "production"})
    with pytest.raises(ValueError, match="run_scope is immutable"):
        ledger.update(run_id, run_scope="acceptance")
    claim = ledger.claim_next_job()
    ledger.finish_job(claim["job_id"], run_id, {"status": "pass"})
    with pytest.raises(ValueError, match="run_scope is immutable"):
        ledger.update(run_id, run_scope="migration")


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


def test_managed_publication_and_ledger_recovery_stay_in_scoped_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DATACENTER_PROVIDER_ALLOWLIST", "dukascopy,fixture")
    monkeypatch.setenv("DATACENTER_DATA_MODE", "fixture")
    lake = tmp_path / "lake"
    center = DatasetCenter(lake)
    center.create(dataset_id="managed", name="Managed")
    center.add_member(
        "managed", DatasetMember(symbol="EURUSD"), expected_version=1)
    scoped = managed_dataset_root(lake, "managed")
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    worker = LocalWorker(lake, ledger)
    run_id = worker.submit(IngestJob(
        job_id="managed-recover", managed_dataset_id="managed", provider="dukascopy",
        symbol="EURUSD", asset_class="fx", timeframe="1m",
        start=datetime(2025, 12, 31, tzinfo=timezone.utc),
        end=datetime(2025, 12, 31, 1, tzinfo=timezone.utc),
    ))
    finish = ledger.finish_job
    monkeypatch.setattr(
        ledger, "finish_job",
        lambda *args: (_ for _ in ()).throw(OSError("ledger unavailable")),
    )

    with pytest.raises(OSError, match="ledger unavailable"):
        worker.run_next()

    assert ledger.get(run_id)["status"] == "running"
    assert manifest_path(scoped, run_id).exists()
    assert not manifest_path(lake, run_id).exists()
    assert (scoped / ".ingest-staging" / run_id / "1" / "result.json").exists()
    assert not (lake / ".ingest-staging" / run_id).exists()

    monkeypatch.setattr(ledger, "finish_job", finish)
    assert worker.run_next() is False
    assert ledger.get(run_id)["status"] == "pass"


def test_archived_dataset_blocks_a_previously_queued_worker_job(tmp_path):
    lake = tmp_path / "lake"
    center = DatasetCenter(lake)
    center.create(dataset_id="managed", name="Managed")
    center.add_member(
        "managed", DatasetMember(symbol="EURUSD"), expected_version=1)
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    worker = LocalWorker(lake, ledger)
    run_id = worker.submit(IngestJob(
        job_id="archived-before-claim", managed_dataset_id="managed", provider="fixture",
        symbol="EURUSD", asset_class="fx", timeframe="1d",
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, tzinfo=timezone.utc),
    ))
    center.update("managed", expected_version=2, status="archived")

    assert worker.run_next() is True
    receipt = ledger.get(run_id)
    assert receipt["status"] == "failed"
    assert receipt["failure_stage"] == "ownership"
    assert not (managed_dataset_root(lake, "managed") / ".ingest-staging" / run_id).exists()


def test_worker_rejects_a_queued_job_outside_managed_membership(tmp_path):
    lake = tmp_path / "lake"
    center = DatasetCenter(lake)
    center.create(dataset_id="managed", name="Managed")
    center.add_member(
        "managed", DatasetMember(symbol="EURUSD"), expected_version=1)
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    run_id = ledger.enqueue_job({
        "job_id": "invalid-member", "managed_dataset_id": "managed",
        "dataset_id": "provider_bars", "provider": "dukascopy", "symbol": "GBPUSD",
        "asset_class": "fx", "timeframe": "1m", "price_basis": "bid",
        "start": "2026-01-01T00:00:00+00:00", "end": "2026-01-01T01:00:00+00:00",
    })

    assert LocalWorker(lake, ledger).run_next() is True
    receipt = ledger.get(run_id)
    assert receipt["status"] == "failed"
    assert receipt["failure_stage"] == "ownership"
    assert not (managed_dataset_root(lake, "managed") / ".ingest-staging" / run_id).exists()


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


def test_dead_letter_acknowledgment_and_resolution_preserve_terminal_run(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    run_id = ledger.enqueue_job({"job_id": "original", "dataset_id": "provider_bars", "run_scope": "production"})
    claim = ledger.claim_next_job()
    for _ in range(2):
        ledger.fail_job(claim["job_id"], run_id, "retry", delay_seconds=0)
        claim = ledger.claim_next_job()
    ledger.fail_job(claim["job_id"], run_id, "terminal")
    original = ledger.get(run_id)
    assert original["dead_letter_state"]["state"] == "active"
    assert ledger.acknowledge_dead_letter(run_id)["dead_letter_state"]["state"] == "acknowledged"
    retry_id = ledger.retry_run(run_id)
    retry = ledger.claim_next_job()
    ledger.finish_job(retry["job_id"], retry_id, {"status": "pass"})
    resolved = ledger.get(run_id)
    assert resolved["status"] == "dead_letter" and resolved["error"] == original["error"]
    assert resolved["dead_letter_state"]["resolved_by_run_id"] == retry_id
    assert resolved["dead_letter_state"]["resolved_at"] is not None
    assert [entry["action"] for entry in ledger.dead_letter_audit(run_id)] == ["acknowledged", "resolved"]
    with __import__("pytest").raises(ValueError, match="cannot return"):
        ledger.acknowledge_dead_letter(run_id)
