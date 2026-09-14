"""Pure scheduling decisions used by the future scheduler process.

The module deliberately has no I/O: persistence and dispatch are layered on
top so shadow ticks can be tested deterministically before activation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def next_run_at(*, schedule: str, now: datetime, anchor: datetime | None = None,
                interval_seconds: int | None = None, run_at: datetime | None = None) -> datetime | None:
    now = now.astimezone(timezone.utc)
    if schedule == "manual":
        return None
    if schedule == "once":
        return run_at.astimezone(timezone.utc) if run_at and run_at >= now else None
    if schedule in {"fixed_rate", "interval"}:
        if not anchor or not interval_seconds or interval_seconds <= 0:
            raise ValueError("interval schedules require positive interval and anchor")
        anchor = anchor.astimezone(timezone.utc)
        if anchor > now:
            return anchor
        steps = int((now - anchor).total_seconds() // interval_seconds) + 1
        return anchor + timedelta(seconds=steps * interval_seconds)
    raise ValueError(f"unsupported schedule: {schedule}")


def reconcile_due(*, now: datetime, desired_state: str, next_at: datetime | None,
                  has_active_execution: bool = False) -> dict:
    """Return an auditable, bounded decision for one plan at a scheduler tick."""
    now = now.astimezone(timezone.utc)
    if desired_state != "enabled":
        return {"action": "hold", "reason": "paused" if desired_state == "paused" else "archived"}
    if has_active_execution:
        return {"action": "hold", "reason": "execution_in_progress"}
    if next_at is None or next_at > now:
        return {"action": "hold", "reason": "not_due"}
    return {"action": "start_execution", "scheduled_for": next_at.isoformat(),
            "late_by_seconds": max(0.0, (now - next_at).total_seconds())}
