from datetime import datetime, timezone

from data_center.scheduler import next_run_at, reconcile_due


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
