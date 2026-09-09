from datetime import datetime, timezone

from data_center.domain.models import IngestJob
from data_center.ingest.worker import LocalWorker
from data_center.runs.ledger import RunLedger


def test_worker_consumes_durable_queued_run(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "audit.sqlite")
    worker = LocalWorker(tmp_path / "lake", ledger)
    job = IngestJob(job_id="worker-test", symbol="BTCUSDT", start=datetime(2026, 1, 1, tzinfo=timezone.utc), end=datetime(2026, 1, 2, tzinfo=timezone.utc))
    run_id = worker.submit(job)
    assert ledger.get(run_id)["status"] == "queued"
    assert worker.run_next() is True
    assert ledger.get(run_id)["status"] == "pass"
    assert worker.run_next() is False


def test_worker_records_economic_failure_with_same_run_id(tmp_path, monkeypatch) -> None:
    ledger = RunLedger(tmp_path / "audit.sqlite")
    worker = LocalWorker(tmp_path / "lake", ledger)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    run_id = worker.submit_economic(series_id="PAYEMS")
    assert worker.run_next() is True
    assert ledger.get(run_id)["status"] == "queued"
    assert ledger.get(run_id)["error_type"] == "RuntimeError"
