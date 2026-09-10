"""Stable metrics and a local, idempotent alert outbox; no data writes depend on delivery."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def run_metrics(ledger) -> dict:
    runs = ledger.list()
    now = datetime.now(timezone.utc)
    counts = {status: sum(r.get('status') == status for r in runs)
              for status in ('queued', 'running', 'pass', 'failed', 'dead_letter')}
    durations = [(datetime.fromisoformat(r['finished_at']) - datetime.fromisoformat(r['started_at'])).total_seconds()
                 for r in runs if r.get('finished_at') and r.get('started_at')]
    queue_ages = [(now - datetime.fromisoformat(r['created_at'])).total_seconds()
                 for r in runs if r.get('status') == 'queued']
    terminal = sum(counts[s] for s in ('pass', 'failed', 'dead_letter'))
    return {'runs_total': len(runs), 'runs_by_status': counts,
            'retry_attempts_total': sum(r.get('retry_count', 0) for r in runs),
            'timeouts_total': sum(sum(e.get('error_type') == 'TimeoutError' for e in r.get('attempt_errors', [])) for r in runs),
            'worker_heartbeat_age_seconds': ledger.heartbeat_age_seconds(),
            'queue_depth': counts['queued'], 'queue_oldest_age_seconds': max(queue_ages, default=0.0),
            'dead_letter_total': counts['dead_letter'], 'success_rate': counts['pass'] / terminal if terminal else None,
            'duration_seconds': {'count': len(durations), 'sum': sum(durations),
                                 'max': max(durations, default=0.0),
                                 'mean': sum(durations) / len(durations) if durations else None}}


class AlertSink:
    """Local durable events with a unique event ID; an external sender may drain the outbox."""

    def __init__(self, root: Path, enabled: bool = True):
        self.root, self.enabled = root, enabled

    def emit(self, event: str, *, identity: str, fields: dict) -> str | None:
        if not self.enabled:
            return None
        # Accept only explicit operational fields, never arbitrary exception strings/URLs.
        allowed = {'run_id', 'job_id', 'request_id', 'attempt', 'provider', 'status', 'error_type',
                   'failure_stage', 'queue_depth', 'receipt', 'providers', 'age_seconds'}
        safe = {key: value for key, value in fields.items() if key in allowed}
        event_id = hashlib.sha256(f'{event}:{identity}'.encode()).hexdigest()
        payload = {'event_id': event_id, 'event': event, 'created_at': datetime.now(timezone.utc).isoformat(),
                   'request_id': None, **safe}
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(self.root / 'alerts.sqlite') as conn:
            conn.execute('create table if not exists events(event_id text primary key, payload text not null)')
            conn.execute('insert or ignore into events values (?,?)', (event_id, json.dumps(payload, sort_keys=True)))
        return event_id

    def events(self) -> list[dict]:
        if not (self.root / 'alerts.sqlite').exists():
            return []
        with sqlite3.connect(self.root / 'alerts.sqlite') as conn:
            return [json.loads(row[0]) for row in conn.execute('select payload from events order by rowid')]


def check_alerts(ledger, sink: AlertSink, *, heartbeat_limit=60, backlog_limit=300) -> list[str]:
    metrics = run_metrics(ledger)
    event_ids = []
    age = metrics['worker_heartbeat_age_seconds']
    # Bucket repeated monitoring evaluations while retaining a stable identity within the bucket.
    bucket = str(int(datetime.now(timezone.utc).timestamp()) // 3600)
    if age is None or age >= heartbeat_limit:
        event_ids.append(sink.emit('worker_heartbeat_expired', identity=bucket,
                                   fields={'age_seconds': age, 'status': 'not_ready'}))
    if metrics['queue_depth'] and metrics['queue_oldest_age_seconds'] >= backlog_limit:
        event_ids.append(sink.emit('queue_backlog', identity=bucket,
                                   fields={'queue_depth': metrics['queue_depth'],
                                           'age_seconds': metrics['queue_oldest_age_seconds'], 'status': 'warning'}))
    for run in ledger.list():
        if (run.get('quality_summary') or {}).get('status') == 'fail':
            event_ids.append(sink.emit('quality_failed', identity=run['run_id'], fields=run))
    return [event_id for event_id in event_ids if event_id]


def deliver_alerts(sink: AlertSink, webhook_url: str | None = None, *, max_attempts: int = 3,
                   backoff_seconds: float = 0.0) -> dict:
    """Explicit maintenance delivery. Receiver deduplicates by Idempotency-Key after a crash."""
    import requests
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if not webhook_url or not sink.enabled:
        return {'status': 'disabled', 'delivered': 0, 'failed': 0}
    sent = failed = 0
    events = sink.events()
    with sqlite3.connect(sink.root / 'alerts.sqlite') as conn:
        conn.execute('create table if not exists deliveries(event_id text primary key, delivered_at text not null)')
        for event in events:
            if conn.execute('select 1 from deliveries where event_id=?', (event['event_id'],)).fetchone():
                continue
            delivered = False
            for attempt in range(max_attempts):
                try:
                    response = requests.post(webhook_url, json=event, timeout=5,
                                             headers={'Idempotency-Key': event['event_id']}, allow_redirects=False)
                    if not 200 <= response.status_code < 300:
                        raise RuntimeError('alert delivery failed')
                    delivered = True
                    break
                except (requests.RequestException, RuntimeError):
                    if backoff_seconds and attempt + 1 < max_attempts:
                        import time
                        time.sleep(backoff_seconds * (2 ** attempt))
            if not delivered:
                failed += 1
                continue
            conn.execute('insert or ignore into deliveries values (?,?)',
                         (event['event_id'], datetime.now(timezone.utc).isoformat()))
            conn.commit()
            sent += 1
    return {'status': 'failed' if failed else 'pass', 'delivered': sent, 'failed': failed}


def main():
    from data_center.runs.ledger import RunLedger
    from data_center.settings import Settings
    settings = Settings()
    sink = AlertSink(settings.evidence_root / 'alerts', enabled=settings.alerts_enabled)
    events = check_alerts(RunLedger(settings.ledger_path), sink)
    delivery = deliver_alerts(sink, settings.alert_webhook_url)
    print(json.dumps({'event': 'monitor_completed', 'request_id': None,
                      'event_ids': events, 'delivery': delivery}), flush=True)


if __name__ == '__main__':
    main()
