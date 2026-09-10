from datetime import datetime, timezone

from fastapi.testclient import TestClient

from data_center.api.app import create_app
from data_center.capacity import CapacityPolicy, CapacitySnapshot
from data_center.domain.models import IngestJob
from data_center.ingest.worker import LocalWorker
from data_center.observability import AlertSink, check_alerts
from data_center.operations import backfill
from data_center.runs.ledger import RunLedger
from data_center.settings import Settings


def snapshot(status: str, ratio: float) -> CapacitySnapshot:
    return CapacitySnapshot(100, int((1 - ratio) * 100), int(ratio * 100), ratio, status, 0.15, 0.10)


class FixedPolicy(CapacityPolicy):
    def __init__(self, state: CapacitySnapshot):
        object.__setattr__(self, "warning_free_ratio", state.warning_free_ratio)
        object.__setattr__(self, "critical_free_ratio", state.critical_free_ratio)
        object.__setattr__(self, "state", state)

    def inspect(self, path):
        return self.state


def test_capacity_threshold_boundaries():
    policy = CapacityPolicy(0.15, 0.10)
    assert policy.classify(0.15) == "ok"
    assert policy.classify(0.10) == "warning"
    assert policy.classify(0.0999) == "critical"


def test_settings_reject_invalid_capacity_thresholds():
    import pytest

    with pytest.raises(ValueError, match="critical < warning"):
        Settings(capacity_warning_free_ratio=0.10, capacity_critical_free_ratio=0.10)


def test_critical_capacity_rejects_api_ingest_but_keeps_reads(monkeypatch, tmp_path):
    policy = FixedPolicy(snapshot("critical", 0.09))
    monkeypatch.setattr(Settings, "capacity_policy", lambda self: policy)
    settings = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "lake/audit/ledger.sqlite",
                        evidence_root=tmp_path / "evidence")
    ledger = RunLedger(settings.ledger_path)
    ledger.heartbeat()
    client = TestClient(create_app(settings))
    ready = client.get("/api/v1/health/ready")
    assert ready.status_code == 200
    assert ready.json()["data"]["status"] == "ready"
    assert ready.json()["data"]["read_status"] == "available"
    assert ready.json()["data"]["write_status"] == "protected"
    job = {"job_id": "critical", "symbol": "TEST", "start": "2026-01-01T00:00:00Z",
           "end": "2026-01-02T00:00:00Z"}
    response = client.post("/api/v1/ingest/runs", json=job)
    assert response.status_code == 507
    assert response.json()["errors"][0]["code"] == "capacity_protected"
    ledger.enqueue_job({"job_id": "failed", "dataset_id": "provider_bars"})
    claimed = ledger.claim_next_job()
    ledger.fail_job(claimed["job_id"], claimed["run_id"], "failed", retryable=False)
    assert client.post(f"/api/v1/runs/{claimed['run_id']}/retry").status_code == 507
    assert client.get("/api/v1/datasets").status_code == 200


def test_critical_capacity_worker_leaves_job_queued(tmp_path):
    root = tmp_path / "lake"
    ledger = RunLedger(root / "audit/ledger.sqlite")
    worker = LocalWorker(root, ledger, capacity_policy=FixedPolicy(snapshot("critical", 0.09)))
    run_id = worker.submit(IngestJob(job_id="queued", symbol="TEST",
                                     start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                     end=datetime(2026, 1, 2, tzinfo=timezone.utc)))
    assert worker.run_next() is False
    assert ledger.get(run_id)["status"] == "queued"


def test_capacity_alert_is_idempotent_across_monitor_runs(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    sink = AlertSink(tmp_path / "alerts")
    policy = FixedPolicy(snapshot("warning", 0.12))
    first = check_alerts(ledger, sink, canonical_root=tmp_path, capacity_policy=policy)
    second = check_alerts(ledger, sink, canonical_root=tmp_path, capacity_policy=policy)
    capacity_events = [event for event in sink.events() if event["event"] == "capacity_warning"]
    assert len(capacity_events) == 1
    assert len(first) == 2 and second == []

    ok_policy = FixedPolicy(snapshot("ok", 0.20))
    check_alerts(ledger, sink, canonical_root=tmp_path, capacity_policy=ok_policy)
    third = check_alerts(ledger, sink, canonical_root=tmp_path, capacity_policy=policy)
    capacity_events = [event for event in sink.events() if event["event"] == "capacity_warning"]
    assert len(third) == 1 and third[0] != first[0]
    assert len(capacity_events) == 2


def test_warning_capacity_blocks_large_unattended_backfill_before_network(tmp_path):
    from datetime import date

    policy = FixedPolicy(snapshot("warning", 0.12))
    with __import__("pytest").raises(RuntimeError, match="over 31 days"):
        backfill(
            "http://127.0.0.1:1", "fixture", "TEST", "test",
            date(2026, 1, 1), date(2026, 3, 1), tmp_path / "receipt.json",
            canonical_root=tmp_path, capacity_policy=policy,
        )
    assert not (tmp_path / "receipt.json").exists()
