from datetime import datetime, timezone

from data_center.scheduler import next_run_at, reconcile_due
from data_center.runs.ledger import RunLedger
from data_center.scheduler import Scheduler


def test_fixed_rate_aligns_to_anchor_and_manual_has_no_next_run():
    now = datetime(2026, 9, 14, 12, 7, tzinfo=timezone.utc)
    anchor = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    assert next_run_at(schedule="fixed_rate", now=now, anchor=anchor, interval_seconds=900) == datetime(2026, 9, 14, 12, 15, tzinfo=timezone.utc)
    assert next_run_at(schedule="manual", now=now) is None


def test_reconcile_is_bounded_and_pause_wins():
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    due = datetime(2026, 9, 14, 11, 59, tzinfo=timezone.utc)
    assert reconcile_due(now=now, desired_state="paused", next_at=due)["action"] == "hold"
    assert reconcile_due(now=now, desired_state="enabled", next_at=due)["action"] == "start_execution"
    assert reconcile_due(now=now, desired_state="enabled", next_at=due, has_active_execution=True)["reason"] == "execution_in_progress"


def test_scheduler_shadow_tick_and_lease_fencing(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    ledger.create_production_task(task_id="p", name="A", payload={"next_run_at": "2026-09-14T11:59:00+00:00"}, ownership_keys=["k"], desired_state="enabled")
    scheduler = Scheduler(ledger, instance_id="one")
    result = scheduler.tick(now=datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc))
    assert result["decisions"][0]["action"] == "shadow_start_execution"
    assert ledger.acquire_scheduler_lease("global", "one") == 1
    assert ledger.acquire_scheduler_lease("global", "two") is None
