import json
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


def test_non_loopback_requires_key(tmp_path):
    with pytest.raises(ValueError, match='DATACENTER_API_KEY is required'):
        Settings(host='0.0.0.0', api_key=None)
    assert Settings(host='192.168.1.2', api_key='test').host == '192.168.1.2'


def test_alerts_idempotent_disabled_and_delivery_failure(tmp_path, monkeypatch):
    sink = AlertSink(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: sink.emit('quality_failed', identity='run-1',
                                                fields={'run_id': 'run-1', 'raw_response': 'secret'}), range(8)))
    assert len(set(ids)) == 1 and len(sink.events()) == 1
    assert 'secret' not in json.dumps(sink.events())
    assert AlertSink(tmp_path / 'disabled', False).emit('x', identity='x', fields={}) is None
    assert not (tmp_path / 'disabled').exists()
    monkeypatch.setattr('requests.post', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('secret')))
    assert deliver_alerts(sink, 'http://example.test') == {'status': 'failed', 'delivered': 0, 'failed': 1}
    assert len(sink.events()) == 1


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


def test_all_write_endpoints_refuse_unauthorized_requests(tmp_path):
    client = TestClient(create_app(Settings(canonical_root=tmp_path/'lake', ledger_path=tmp_path/'ledger', api_key='test-key')))
    job = {'job_id': 'test', 'symbol': 'TEST', 'start': '2026-01-01T00:00:00Z', 'end': '2026-01-02T00:00:00Z'}
    for endpoint in ('/ingest/runs', '/quality/checks'):
        assert client.post('/api/v1'+endpoint, json=job).status_code == 401
    assert client.post('/api/v1/economic/ingest?series_id=PAYEMS').status_code == 401
    assert client.post('/api/v1/runs/missing/retry').status_code == 401
    assert client.get('/api/v1/runs').json()['data'] == []
