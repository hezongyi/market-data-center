import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from data_center.api.app import create_app
from data_center.observability import (
    AlertSink,
    check_alerts,
    deliver_alerts,
    run_metrics,
)
from data_center.runs.ledger import RunLedger
from data_center.settings import Settings
from data_center.snapshot import ReceiptIndex


def test_non_loopback_requires_key(tmp_path):
    with pytest.raises(ValueError, match='DATACENTER_API_KEY is required'):
        Settings(host='0.0.0.0', api_key=None)
    assert Settings(host='192.168.1.2', api_key='test').host == '192.168.1.2'


def test_alerts_idempotent_disabled_and_delivery_failure(tmp_path, monkeypatch):
    sink = AlertSink(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: sink.emit('quality_failed', identity='run-1',
                                                fields={'run_id': 'run-1', 'raw_response': 'secret'}), range(8)))
    assert len([event_id for event_id in ids if event_id]) == 1 and len(sink.events()) == 1
    assert 'secret' not in json.dumps(sink.events())
    assert AlertSink(tmp_path / 'disabled', False).emit('x', identity='x', fields={}) is None
    assert not (tmp_path / 'disabled').exists()
    monkeypatch.setattr('requests.post', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('secret')))
    assert deliver_alerts(sink, 'http://example.test') == {'status': 'failed', 'delivered': 0, 'failed': 1}
    assert len(sink.events()) == 1


def test_alert_delivery_retries_with_stable_idempotency_key(tmp_path, monkeypatch):
    sink = AlertSink(tmp_path)
    sink.emit("quality_failed", identity="run-2", fields={"run_id": "run-2", "request_id": "req-2"})
    calls = []

    class Response:
        status_code = 200

    def post(url, **kwargs):
        calls.append(kwargs["headers"]["Idempotency-Key"])
        if len(calls) < 3:
            raise RuntimeError("temporary")
        return Response()

    monkeypatch.setattr("requests.post", post)
    assert deliver_alerts(sink, "http://example.test", max_attempts=3) == {
        "status": "pass", "delivered": 1, "failed": 0,
    }
    assert len(calls) == 3 and len(set(calls)) == 1


def test_delivery_batch_selects_undelivered_events_before_limiting(tmp_path, monkeypatch):
    sink = AlertSink(tmp_path)
    event_ids = [sink.emit("quality_failed", identity=f"run-{number}", fields={"run_id": f"run-{number}"})
                 for number in range(3)]
    with sqlite3.connect(tmp_path / "alerts.sqlite") as database:
        database.execute("create table deliveries(event_id text primary key, delivered_at text not null)")
        database.executemany("insert into deliveries values (?,?)", ((event_id, "done") for event_id in event_ids[:2]))
    delivered = []

    class Response:
        status_code = 204

    monkeypatch.setattr("requests.post", lambda url, **kwargs: (delivered.append(kwargs["headers"]["Idempotency-Key"]), Response())[1])
    assert deliver_alerts(sink, "http://example.test", batch_size=1) == {
        "status": "pass", "delivered": 1, "failed": 0,
    }
    assert delivered == [event_ids[2]]


def test_delivery_timeout_and_retries_are_bounded_by_total_budget(tmp_path, monkeypatch):
    sink = AlertSink(tmp_path)
    sink.emit("quality_failed", identity="run-budget", fields={"run_id": "run-budget"})
    timeouts = []

    def post(url, **kwargs):
        timeouts.append(kwargs["timeout"])
        time.sleep(0.03)
        raise RuntimeError("unavailable")

    monkeypatch.setattr("requests.post", post)
    started = time.monotonic()
    result = deliver_alerts(
        sink, "http://example.test", max_attempts=10, timeout_seconds=5.0, budget_seconds=0.05,
    )
    assert time.monotonic() - started < 0.12
    assert result == {"status": "failed", "delivered": 0, "failed": 1}
    assert timeouts and max(timeouts) <= 0.05


def test_metrics_and_monitor_detect_backlog_and_quality(tmp_path):
    ledger = RunLedger(tmp_path / 'ledger.sqlite')
    run_id = ledger.enqueue_job({'job_id': 'job', 'dataset_id': 'provider_bars'})
    ledger.update(run_id, created_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())
    sink = AlertSink(tmp_path / 'alerts')
    check_alerts(ledger, sink)
    assert {e['event'] for e in sink.events()} == {'queue_backlog', 'worker_heartbeat_expired'}
    claim = ledger.claim_next_job()
    ledger.fail_job(claim['job_id'], run_id, 'quality failed', retryable=False,
                    quality_summary={'status': 'fail', 'finding_count': 1, 'findings': []})
    check_alerts(ledger, sink)
    metrics = run_metrics(ledger)
    assert metrics['queue_depth'] == 0 and metrics['success_rate'] == 0
    assert metrics['duration_seconds']['count'] == 1
    assert any(e['event'] == 'quality_failed' and e['run_id'] == run_id for e in sink.events())


def test_metrics_include_capacity_backup_recovery_and_temporary_counts(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    evidence = tmp_path / "evidence"
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    (backup_root / ".incomplete.partial").write_bytes(b"partial")
    for action, completed in (("backup", "2026-09-10T01:00:00+00:00"),
                              ("recovery_drill", "2026-09-10T02:00:00+00:00")):
        target = evidence / "operations" / action / "receipt.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"receipt_id": action, "action": action, "result": "pass",
                                      "completed_at": completed, "details": {}}))
    index = ReceiptIndex(evidence)
    assert index.rebuild()["indexed"] == 2
    metrics = run_metrics(ledger, canonical_root=tmp_path, evidence_root=evidence,
                          backup_root=backup_root, receipt_index=index)
    assert metrics["capacity"]["status"] in {"ok", "warning", "critical"}
    assert metrics["temporary_backup_count"] == 1
    assert metrics["last_successful_backup_at"] == "2026-09-10T01:00:00+00:00"
    assert metrics["last_successful_recovery_drill_at"] == "2026-09-10T02:00:00+00:00"


def test_all_write_endpoints_refuse_unauthorized_requests(tmp_path):
    client = TestClient(create_app(Settings(canonical_root=tmp_path/'lake', ledger_path=tmp_path/'ledger',
                                            evidence_root=tmp_path/'evidence', api_key='test-key')))
    job = {'job_id': 'test', 'symbol': 'TEST', 'start': '2026-01-01T00:00:00Z', 'end': '2026-01-02T00:00:00Z'}
    for endpoint in ('/ingest/runs', '/quality/checks'):
        assert client.post('/api/v1'+endpoint, json=job).status_code == 401
    assert client.post('/api/v1/economic/ingest?series_id=PAYEMS').status_code == 401
    assert client.post('/api/v1/runs/missing/retry').status_code == 401
    assert client.post('/api/v1/runs/missing/acknowledge').status_code == 401
    assert client.get('/api/v1/runs').json()['data'] == []


def test_corrupt_index_marks_readiness_stale_without_blocking_reads(tmp_path):
    settings = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "ledger.sqlite",
                        evidence_root=tmp_path / "evidence")
    ledger = RunLedger(settings.ledger_path)
    ledger.heartbeat()
    client = TestClient(create_app(settings))
    (settings.evidence_root / "receipt-index.sqlite").write_bytes(b"corrupt")

    ready = client.get("/api/v1/health/ready")
    assert ready.status_code == 503
    assert ready.json()["data"]["read_status"] == "available"
    assert ready.json()["data"]["operational_snapshot_status"] == "stale"
    assert client.get("/api/v1/datasets").status_code == 200
    metrics = client.get("/api/v1/metrics")
    assert metrics.status_code == 200
    assert metrics.json()["data"]["operational_snapshot_status"] == "stale"


def test_acceptance_failure_does_not_change_production_success_rate(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    production_id = ledger.enqueue_job({"job_id": "production", "dataset_id": "provider_bars",
                                        "run_scope": "production"})
    production = ledger.claim_next_job()
    ledger.finish_job(production["job_id"], production_id, {"status": "pass"})
    acceptance_id = ledger.enqueue_job({"job_id": "acceptance", "dataset_id": "provider_bars",
                                        "run_scope": "acceptance"})
    acceptance = ledger.claim_next_job()
    ledger.fail_job(acceptance["job_id"], acceptance_id, "intentional", retryable=False,
                    error_type="AcceptanceFailure")
    metrics = run_metrics(ledger)
    assert metrics["production"]["success_rate"] == 1.0
    assert metrics["success_rate"] == 1.0
    assert metrics["legacy_lifetime_success_rate"] == 0.5
    assert metrics["runs_by_scope"] == {"production": 1, "acceptance": 1, "migration": 0,
                                         "maintenance": 0, "legacy_unclassified": 0}
    assert metrics["failures_by_error_category"] == {}
    assert metrics["lifetime_failures_by_error_category"] == {"AcceptanceFailure": 1}
