"""Stable metrics and a local, idempotent alert outbox; no data writes depend on delivery."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from data_center.capacity import CapacityPolicy
from data_center.snapshot import ReceiptIndex, build_snapshot


def _latest_success(root: Path | None, action: str) -> str | None:
    if root is None or not root.exists():
        return None
    latest = None
    # Kept for explicit maintenance/backward compatibility only.  Hot paths use
    # ReceiptIndex and never recurse through evidence or shared storage.
    for path in root.glob(f"operations/{action}/*.json"):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("action") == action and payload.get("result") == "pass":
            latest = max(filter(None, (latest, payload.get("completed_at"))), default=None)
    return latest


def run_metrics(ledger, *, canonical_root: Path | None = None, evidence_root: Path | None = None,
                capacity_policy: CapacityPolicy | None = None, receipt_index: ReceiptIndex | None = None,
                backup_root: Path | None = None, restore_staging_root: Path | None = None) -> dict:
    snapshot = build_snapshot(
        ledger, capacity_policy=capacity_policy or (CapacityPolicy() if canonical_root else None),
        canonical_root=canonical_root, receipt_index=receipt_index,
        backup_root=backup_root, restore_staging_root=restore_staging_root,
        evidence_root=evidence_root,
    )
    payload = dict(snapshot.metrics)
    payload["capacity"] = snapshot.capacity
    payload["temporary_backup_count"] = snapshot.temporary_artifacts["count"]
    payload["temporary_artifacts_status"] = snapshot.temporary_artifacts["status"]
    payload["last_successful_backup_at"] = snapshot.last_successful_backup_at
    payload["last_successful_recovery_drill_at"] = snapshot.last_successful_recovery_drill_at
    payload["operational_snapshot_status"] = snapshot.status
    payload["snapshot_generated_at"] = snapshot.generated_at
    payload["snapshot_generation_seconds"] = snapshot.generation_seconds
    return payload


class AlertSink:
    """Local durable events with a unique event ID; an external sender may drain the outbox."""

    def __init__(self, root: Path, enabled: bool = True):
        self.root, self.enabled = root, enabled

    def emit(self, event: str, *, identity: str, fields: dict) -> str | None:
        if not self.enabled:
            return None
        # Accept only explicit operational fields, never arbitrary exception strings/URLs.
        allowed = {'run_id', 'job_id', 'request_id', 'attempt', 'provider', 'status', 'error_type',
                   'failure_stage', 'queue_depth', 'receipt', 'providers', 'age_seconds',
                   'free_ratio', 'warning_free_ratio', 'critical_free_ratio'}
        safe = {key: value for key, value in fields.items() if key in allowed}
        event_id = hashlib.sha256(f'{event}:{identity}'.encode()).hexdigest()
        payload = {'event_id': event_id, 'event': event, 'created_at': datetime.now(timezone.utc).isoformat(),
                   'request_id': None, **safe}
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(self.root / 'alerts.sqlite') as conn:
            conn.execute('create table if not exists events(event_id text primary key, payload text not null)')
            inserted = conn.execute(
                'insert or ignore into events values (?,?)', (event_id, json.dumps(payload, sort_keys=True))
            ).rowcount
        return event_id if inserted else None

    def events(self) -> list[dict]:
        if not (self.root / 'alerts.sqlite').exists():
            return []
        with sqlite3.connect(self.root / 'alerts.sqlite') as conn:
            return [json.loads(row[0]) for row in conn.execute('select payload from events order by rowid')]

    def emit_transition(self, name: str, state: str, *, event: str | None, fields: dict) -> str | None:
        """Emit once per state transition, allowing a recovered condition to alert again later."""
        if not self.enabled:
            return None
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(self.root / 'alerts.sqlite') as conn:
            conn.execute('begin immediate')
            conn.execute('create table if not exists events(event_id text primary key, payload text not null)')
            conn.execute(
                'create table if not exists alert_states('
                'name text primary key, state text not null, generation integer not null)'
            )
            row = conn.execute('select state,generation from alert_states where name=?', (name,)).fetchone()
            if row and row[0] == state:
                return None
            generation = (row[1] + 1) if row else 1
            conn.execute(
                'insert into alert_states values (?,?,?) on conflict(name) do update set '
                'state=excluded.state,generation=excluded.generation',
                (name, state, generation),
            )
            if event is None:
                return None
            allowed = {'status', 'free_ratio', 'warning_free_ratio', 'critical_free_ratio'}
            safe = {key: value for key, value in fields.items() if key in allowed}
            event_id = hashlib.sha256(f'{event}:{name}:{state}:{generation}'.encode()).hexdigest()
            payload = {
                'event_id': event_id, 'event': event,
                'created_at': datetime.now(timezone.utc).isoformat(), 'request_id': None, **safe,
            }
            conn.execute('insert into events values (?,?)', (event_id, json.dumps(payload, sort_keys=True)))
            return event_id


class MonitorEvaluator:
    """Evaluate a snapshot with a deadline; delivery is intentionally separate."""
    def __init__(self, *, runtime_max_seconds: float = 30.0):
        self.runtime_max_seconds = runtime_max_seconds

    def evaluate(self, snapshot: dict, *, now: datetime | None = None) -> dict:
        started = datetime.now(timezone.utc)
        events = []
        capacity = snapshot.get("capacity") or {}
        if capacity.get("status") in {"warning", "critical"}:
            events.append({"event": f"capacity_{capacity['status']}", "idempotency_key": f"capacity:{capacity['status']}"})
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        if elapsed > self.runtime_max_seconds:
            events.append({"event": "monitor_runtime_exceeded", "idempotency_key": "monitor_runtime_exceeded"})
        return {"events": events, "candidate_event_count": len(events), "evaluation_duration_seconds": elapsed,
                "snapshot_generated_at": snapshot.get("generated_at"), "timed_out": elapsed > self.runtime_max_seconds}


class WebhookDelivery:
    def __init__(self, *, batch_size: int = 50, timeout_seconds: float = 5.0, budget_seconds: float = 20.0):
        self.batch_size, self.timeout_seconds, self.budget_seconds = batch_size, timeout_seconds, budget_seconds

    def send(self, events: list[dict], webhook_url: str | None) -> dict:
        if not webhook_url:
            return {"sent": 0, "failed": 0, "status": "disabled"}
        import requests
        started = time.monotonic()
        sent = failed = 0
        for event in events[:self.batch_size]:
            if time.monotonic() - started >= self.budget_seconds:
                break
            try:
                response = requests.post(webhook_url, json=event, timeout=self.timeout_seconds,
                                         headers={"Idempotency-Key": event.get("idempotency_key", "")}, allow_redirects=False)
                if 200 <= response.status_code < 300:
                    sent += 1
                else:
                    failed += 1
            except requests.RequestException:
                failed += 1
        return {"sent": sent, "failed": failed, "status": "failed" if failed else "pass"}


def check_alerts(ledger, sink: AlertSink, *, heartbeat_limit=60, backlog_limit=300,
                 canonical_root: Path | None = None,
                 capacity_policy: CapacityPolicy | None = None,
                 receipt_index: ReceiptIndex | None = None,
                 evidence_root: Path | None = None, backup_root: Path | None = None,
                 restore_staging_root: Path | None = None,
                 metrics: dict | None = None) -> list[str]:
    metrics = metrics or run_metrics(
            ledger, canonical_root=canonical_root, capacity_policy=capacity_policy,
            receipt_index=receipt_index, evidence_root=evidence_root,
            backup_root=backup_root, restore_staging_root=restore_staging_root,
        )
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
    capacity = metrics.get("capacity")
    if canonical_root is not None:
        capacity_state = capacity["status"] if capacity else "unknown"
        event_ids.append(sink.emit_transition(
            "capacity", capacity_state,
            event=f"capacity_{capacity_state}" if capacity_state in {"warning", "critical"} else None,
            fields=capacity or {"status": "unknown"},
        ))
    for run_id in metrics["quality_failed_runs"]:
        event_ids.append(sink.emit('quality_failed', identity=run_id, fields={"run_id": run_id}))
    return [event_id for event_id in event_ids if event_id]


def deliver_alerts(sink: AlertSink, webhook_url: str | None = None, *, max_attempts: int = 3,
                   backoff_seconds: float = 0.0, batch_size: int = 50,
                   timeout_seconds: float = 5.0, budget_seconds: float = 20.0) -> dict:
    """Explicit maintenance delivery. Receiver deduplicates by Idempotency-Key after a crash."""
    import requests
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if not webhook_url or not sink.enabled:
        return {'status': 'disabled', 'delivered': 0, 'failed': 0}
    sent = failed = 0
    started = time.monotonic()
    with sqlite3.connect(sink.root / 'alerts.sqlite') as conn:
        conn.execute('create table if not exists deliveries(event_id text primary key, delivered_at text not null)')
        rows = conn.execute(
            'select events.payload from events left join deliveries using(event_id) '
            'where deliveries.event_id is null order by events.rowid limit ?',
            (batch_size,),
        ).fetchall()
        for row in rows:
            event = json.loads(row[0])
            if time.monotonic() - started >= budget_seconds:
                break
            delivered = False
            for attempt in range(max_attempts):
                remaining = budget_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    break
                try:
                    response = requests.post(webhook_url, json=event, timeout=min(timeout_seconds, remaining),
                                             headers={'Idempotency-Key': event['event_id']}, allow_redirects=False)
                    if not 200 <= response.status_code < 300:
                        raise RuntimeError('alert delivery failed')
                    delivered = True
                    break
                except (requests.RequestException, RuntimeError):
                    if backoff_seconds and attempt + 1 < max_attempts:
                        remaining = budget_seconds - (time.monotonic() - started)
                        if remaining > 0:
                            time.sleep(min(backoff_seconds * (2 ** attempt), remaining))
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
    from data_center.deployment import validated_runtime_identity
    identity = validated_runtime_identity(
        settings.deployment_manifest, settings.evidence_root, component="monitor",
    ) if settings.deployment_manifest else {
        "deployment_id": "development", "software_version": "unknown", "source_commit": "unknown"
    }
    sink = AlertSink(settings.evidence_root / 'alerts', enabled=settings.alerts_enabled)
    index = ReceiptIndex(settings.evidence_root)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    ledger = RunLedger(settings.ledger_path)
    metrics = run_metrics(
        ledger, canonical_root=settings.canonical_root, capacity_policy=settings.capacity_policy(),
        receipt_index=index, evidence_root=settings.evidence_root, backup_root=settings.backup_root,
        restore_staging_root=settings.restore_staging_root,
    )
    evaluation_started = time.monotonic()
    events = check_alerts(
        ledger, sink, canonical_root=settings.canonical_root,
        capacity_policy=settings.capacity_policy(), receipt_index=index,
        evidence_root=settings.evidence_root, backup_root=settings.backup_root,
        restore_staging_root=settings.restore_staging_root, metrics=metrics,
    )
    evaluation_duration = time.monotonic() - evaluation_started
    delivery_started = time.monotonic()
    delivery = deliver_alerts(
        sink, settings.alert_webhook_url, batch_size=settings.monitor_delivery_batch_size,
        timeout_seconds=settings.monitor_delivery_timeout_seconds,
        budget_seconds=settings.monitor_delivery_budget_seconds,
    )
    delivery_duration = time.monotonic() - delivery_started
    total_duration = time.monotonic() - started
    exceeded = total_duration > settings.monitor_runtime_max_seconds
    if exceeded:
        sink.emit("monitor_runtime_exceeded", identity=started_at[:16], fields={"status": "warning"})
    from data_center.evidence import operation_receipt, write_receipt
    receipt = operation_receipt(
        action="monitor", command="data_center.observability", started_at=started_at,
        result="failed" if exceeded else "pass",
        failure_stage="runtime_deadline" if exceeded else None,
        error_category="MonitorRuntimeExceeded" if exceeded else None,
        details={
            "deployment_id": identity["deployment_id"],
            "software_version": identity["software_version"],
            "source_commit": identity["source_commit"],
            "snapshot_age_seconds": 0.0,
            "snapshot_status": metrics["operational_snapshot_status"],
            "snapshot_generation_seconds": metrics["snapshot_generation_seconds"],
            "evaluation_duration_seconds": evaluation_duration,
            "delivery_duration_seconds": delivery_duration,
            "candidate_event_count": len(events),
            "sent_count": delivery.get("delivered", delivery.get("sent", 0)),
            "failed_count": delivery.get("failed", 0),
        },
    )
    receipt_path = write_receipt(settings.evidence_root, receipt)
    print(json.dumps({'event': 'monitor_completed', 'request_id': None, **{k: identity[k] for k in ('deployment_id','software_version','source_commit')},
                      'event_ids': events, 'delivery': delivery, 'receipt': str(receipt_path) if receipt_path else None}), flush=True)


if __name__ == '__main__':
    main()
