"""Operations read models for the v0.4 workbench.

Everything here is derived from records the platform already keeps: the run
ledger, the durable alert outbox and the receipt index.  Nothing is estimated,
and a state the platform never recorded is reported as unknown.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Capacity transitions are recorded by the monitor as alert events; these are
# the only historical capacity states this platform actually persists.
CAPACITY_EVENTS = {"capacity_warning", "capacity_critical", "capacity_recovered", "capacity_ok"}
RECEIPT_ACTIONS = ("backup", "restore", "recovery_drill", "release", "deployment")


def _iso(value) -> str | None:
    return value if isinstance(value, str) else None


def worker_activity(ledger, *, heartbeat_limit_seconds: float = 60.0) -> dict:
    """Worker liveness, in-flight jobs and queue depth, straight from the ledger."""
    age = ledger.heartbeat_age_seconds()
    running = ledger.running_jobs()
    queue = ledger.job_queue_state()
    return {
        "heartbeat_age_seconds": age,
        "heartbeat_status": ("fresh" if age is not None and age < heartbeat_limit_seconds
                             else "stale" if age is not None else "unknown"),
        "heartbeat_limit_seconds": heartbeat_limit_seconds,
        "running_jobs": [{"job_id": job["job_id"], "run_id": job["run_id"], "attempts": job["attempts"]}
                         for job in running],
        "running_count": len(running),
        "queue": queue,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def capacity_history(alert_sink, live: dict, *, limit: int = 50) -> dict:
    """Recorded capacity transitions plus the live measurement."""
    events = []
    if alert_sink is not None:
        for event in alert_sink.events():
            if event.get("event") in CAPACITY_EVENTS:
                events.append({
                    "event": event["event"], "created_at": _iso(event.get("created_at")),
                    "status": event.get("status"), "free_ratio": event.get("free_ratio"),
                    "warning_free_ratio": event.get("warning_free_ratio"),
                    "critical_free_ratio": event.get("critical_free_ratio"),
                })
    events.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return {"live": live, "events": events[:limit], "event_count": len(events),
            "recorded_only": True,
            "note": "Only capacity transitions the monitor recorded are shown; no history is interpolated."}


def operations_receipts(receipt_index, *, actions: tuple[str, ...] = RECEIPT_ACTIONS,
                        limit_per_action: int = 5) -> dict:
    """Backup, restore, drill, release and deployment records from the receipt index."""
    if receipt_index is None or not getattr(receipt_index, "available", False):
        return {"available": False, "receipts": [], "latest": {},
                "note": "The receipt index is unavailable, so no operational record can be shown."}
    receipts = []
    latest: dict[str, dict | None] = {}
    for action in actions:
        history = receipt_index.history(action, limit=limit_per_action)
        latest[action] = history[0] if history else None
        receipts.extend(history)
    receipts.sort(key=lambda item: item.get("completed_at") or "", reverse=True)
    return {"available": True, "receipts": receipts, "latest": latest, "note": None}
