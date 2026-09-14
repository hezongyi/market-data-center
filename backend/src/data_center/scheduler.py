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


class Scheduler:
    """One bounded scheduler tick; dispatch remains opt-in for shadow mode."""

    def __init__(self, ledger, *, instance_id: str, dispatch_enabled: bool = False):
        self.ledger = ledger
        self.instance_id = instance_id
        self.dispatch_enabled = dispatch_enabled

    def tick(self, *, now: datetime | None = None, budget: int = 50) -> dict:
        now = now or datetime.now(timezone.utc)
        self.ledger.scheduler_heartbeat(instance_id=self.instance_id, dispatch_enabled=self.dispatch_enabled)
        decisions = []
        for task in self.ledger.list_production_tasks()[: max(0, budget)]:
            definition = task.get("payload") or {}
            next_at = None
            if definition.get("next_run_at"):
                next_at = datetime.fromisoformat(definition["next_run_at"].replace("Z", "+00:00"))
            decision = reconcile_due(now=now, desired_state=task["desired_state"], next_at=next_at)
            decision["task_id"] = task["task_id"]
            if not self.dispatch_enabled and decision["action"] == "start_execution":
                decision["action"] = "shadow_start_execution"
            decisions.append(decision)
        return {"instance_id": self.instance_id, "dispatch_enabled": self.dispatch_enabled,
                "evaluated": len(decisions), "decisions": decisions, "tick_at": now.isoformat()}
