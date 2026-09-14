from datetime import datetime, timezone

import pytest

from data_center.runs.ledger import RunLedger
from data_center.scheduler import (
    Scheduler,
    next_run_at,
    reconcile_due,
    validate_schedule,
)


def test_fixed_rate_aligns_to_anchor_and_manual_has_no_next_run():
    now = datetime(2026, 9, 14, 12, 7, tzinfo=timezone.utc)
    anchor = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    assert next_run_at(schedule="fixed_rate", now=now, anchor=anchor, interval_seconds=900) == datetime(2026, 9, 14, 12, 15, tzinfo=timezone.utc)
    assert next_run_at(schedule="manual", now=now) is None
    assert next_run_at(schedule="fixed_delay", now=now, completed_at=now, interval_seconds=900) == datetime(2026, 9, 14, 12, 22,  tzinfo=timezone.utc)


def test_fixed_rate_keeps_the_anchor_after_a_late_tick():
    anchor = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    now = datetime(2026, 9, 14, 12, 47, tzinfo=timezone.utc)
    assert next_run_at(schedule="fixed_rate", now=now, anchor=anchor, interval_seconds=900) == datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)


def test_fixed_delay_waits_for_completion_instead_of_faking_a_time():
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    # No completion yet: the next time cannot be expressed as an absolute date.
    assert next_run_at(schedule="fixed_delay", now=now, interval_seconds=900) is None
    completed = datetime(2026, 9, 14, 12, 40, tzinfo=timezone.utc)
    assert next_run_at(schedule="fixed_delay", now=now, completed_at=completed, interval_seconds=900) == datetime(2026, 9, 14, 12, 55, tzinfo=timezone.utc)


def test_daily_schedule_uses_iana_timezone():
    now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    # 12:00 UTC is 07:00 in New York, so 08:00 local is still ahead on the same day.
    result = next_run_at(schedule="daily", now=now, timezone_name="America/New_York", local_time="08:00")
    assert result == datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc)
    # Once the local time has passed, the next occurrence is the following day.
    later = datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc)
    assert next_run_at(schedule="daily", now=later, timezone_name="America/New_York", local_time="08:00") == datetime(2026, 1, 16, 13, 0, tzinfo=timezone.utc)


def test_daily_schedule_shifts_a_missing_dst_time_to_the_first_valid_moment():
    # 2026-03-08 02:30 does not exist in New York; the first legal moment is 03:00 EDT.
    now = datetime(2026, 3, 8, 0, 0, tzinfo=timezone.utc)
    result = next_run_at(schedule="daily", now=now, timezone_name="America/New_York", local_time="02:30")
    assert result == datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)


def test_daily_schedule_runs_a_repeated_dst_time_only_once():
    # 2026-11-01 01:30 happens twice in New York: 05:30 UTC (EDT) then 06:30 UTC (EST).
    before = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
    assert next_run_at(schedule="daily", now=before, timezone_name="America/New_York", local_time="01:30") == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    # After the first occurrence the second one is skipped in favour of the next day.
    between = datetime(2026, 11, 1, 5, 45, tzinfo=timezone.utc)
    assert next_run_at(schedule="daily", now=between, timezone_name="America/New_York", local_time="01:30") == datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)


def test_once_keeps_an_expired_slot_so_the_misfire_is_not_lost():
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    missed = datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc)
    assert next_run_at(schedule="once", now=now, run_at=missed) == missed
    assert next_run_at(schedule="once", now=now, run_at=datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)) == datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)
    decision = reconcile_due(now=now, desired_state="enabled", next_at=missed)
    assert decision["action"] == "start_execution"
    assert decision["late_by_seconds"] == 3600.0


def test_naive_datetimes_are_rejected_everywhere():
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    # Parsed rather than constructed so the naive value is what a client would send.
    naive = datetime.fromisoformat("2026-09-14T13:00")
    with pytest.raises(ValueError):
        next_run_at(schedule="manual", now=naive)
    with pytest.raises(ValueError):
        next_run_at(schedule="once", now=now, run_at=naive)
    with pytest.raises(ValueError):
        next_run_at(schedule="fixed_rate", now=now, anchor=naive, interval_seconds=900)
    with pytest.raises(ValueError):
        next_run_at(schedule="fixed_delay", now=now, completed_at=naive, interval_seconds=900)


def test_validate_schedule_reports_field_level_errors():
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    ok = validate_schedule({"schedule": "fixed_rate", "interval_seconds": 900, "anchor": now.isoformat()}, now=now)
    assert ok["schedule"] == "fixed_rate"
    with pytest.raises(ValueError):
        validate_schedule({"schedule": "fixed_rate", "interval_seconds": 60, "anchor": now.isoformat()}, now=now)
    with pytest.raises(ValueError):
        validate_schedule({"schedule": "fixed_rate", "interval_seconds": -900, "anchor": now.isoformat()}, now=now)
    with pytest.raises(ValueError):
        validate_schedule({"schedule": "once", "run_at": "2026-09-14T11:00:00+00:00"}, now=now)
    with pytest.raises(ValueError):
        validate_schedule({"schedule": "once", "run_at": "2026-09-14T13:00"}, now=now)
    assert validate_schedule({"schedule": "once", "run_at": "2026-09-14T13:00:00+00:00"}, now=now)["schedule"] == "once"
    assert validate_schedule({"schedule": "manual"}, now=now)["schedule"] == "manual"
    with pytest.raises(ValueError):
        validate_schedule({"schedule": "daily", "local_time": "08:00"}, now=now)
    with pytest.raises(ValueError):
        validate_schedule({"schedule": "daily", "timezone": "Mars/Olympus", "local_time": "08:00"}, now=now)


def test_reconcile_is_bounded_and_pause_wins():
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    due = datetime(2026, 9, 14, 11, 59, tzinfo=timezone.utc)
    assert reconcile_due(now=now, desired_state="paused", next_at=due)["action"] == "hold"
    assert reconcile_due(now=now, desired_state="enabled", next_at=due)["action"] == "start_execution"
    assert reconcile_due(now=now, desired_state="enabled", next_at=due, has_active_execution=True)["reason"] == "execution_in_progress"
    assert reconcile_due(now=now, desired_state="enabled", next_at=None)["reason"] == "no_schedule"


def _plan(ledger, *, task_id="p", due="2026-09-14T11:59:00+00:00", schedule=None, state="enabled",
          anchor="2026-09-14T12:00:00+00:00", ownership="k"):
    definition = {"schedule": schedule or {"schedule": "fixed_rate", "interval_seconds": 900,
                                           "anchor": anchor}}
    return ledger.create_production_task(task_id=task_id, name=task_id, payload=definition,
                                         ownership_keys=[ownership], desired_state=state,
                                         next_run_at=due)


def test_scheduler_shadow_tick_and_lease_fencing(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    _plan(ledger)
    scheduler = Scheduler(ledger, instance_id="one")
    result = scheduler.tick(now=datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc))
    assert result["decisions"][0]["action"] == "shadow_start_execution"
    # Shadow mode records the decision without creating an execution.
    assert ledger.list_production_executions("p") == []
    # The next slot is reported so the shadow receipt can be compared with the timer.
    assert result["decisions"][0]["next_run_at"] == "2026-09-14T12:15:00+00:00"
    assert ledger.acquire_scheduler_lease("global", "one") == 2
    assert ledger.acquire_scheduler_lease("global", "two") is None


def test_enabled_tick_claims_one_execution_per_slot(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    _plan(ledger)
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True)
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    first = scheduler.tick(now=now)
    assert first["decisions"][0]["action"] == "execution_claimed"
    assert first["decisions"][0]["execution_created"] is True
    assert len(ledger.list_production_executions("p")) == 1
    # Accepting the slot advances the plan in the same transaction, so the next
    # tick has nothing due instead of claiming the same slot twice.
    assert ledger.get_production_task("p")["next_run_at"] == "2026-09-14T12:15:00+00:00"
    second = scheduler.tick(now=now)
    assert second["decisions"] == []
    assert len(ledger.list_production_executions("p")) == 1


def test_a_late_tick_coalesces_and_returns_to_a_future_anchor(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    _plan(ledger, due="2026-09-14T11:00:00+00:00")
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True)
    result = scheduler.tick(now=datetime(2026, 9, 14, 12, 7, tzinfo=timezone.utc))
    decision = result["decisions"][0]
    # 11:00, 11:15, 11:30, 11:45 and 12:00 were all missed but become one run.
    assert decision["coalesced_count"] == 5
    assert decision["coalesced_from"] == "2026-09-14T11:00:00+00:00"
    assert decision["late_by_seconds"] == 4020.0
    assert ledger.get_production_task("p")["next_run_at"] == "2026-09-14T12:15:00+00:00"


def test_execution_in_progress_blocks_a_second_claim(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    _plan(ledger)
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True)
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    scheduler.tick(now=now)
    # Put the slot back as if another plan revision became due while running.
    ledger.set_task_next_run_at(task_id="p", next_run_at="2026-09-14T12:00:00+00:00")
    decision = scheduler.tick(now=now)["decisions"][0]
    assert decision["reason"] == "execution_in_progress"
    assert len(ledger.list_production_executions("p")) == 1


def test_fenced_owner_cannot_claim_after_losing_the_lease(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    _plan(ledger)
    stale_token = ledger.acquire_scheduler_lease("global", "one", ttl_seconds=0.0)
    fresh_token = ledger.acquire_scheduler_lease("global", "two")
    assert stale_token == 1 and fresh_token == 2
    assert ledger.claim_due_execution(task_id="p", owner_id="one",
                                      scheduled_for="2026-09-14T11:59:00+00:00",
                                      definition_version=1, fencing_token=stale_token) is None
    assert ledger.claim_due_execution(task_id="p", owner_id="two",
                                      scheduled_for="2026-09-14T11:59:00+00:00",
                                      definition_version=1, fencing_token=fresh_token) is not None


def test_once_plan_catches_up_an_expired_slot_then_clears_it(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    _plan(ledger, due="2026-09-14T11:00:00+00:00",
          schedule={"schedule": "once", "run_at": "2026-09-14T11:00:00+00:00"})
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True)
    result = scheduler.tick(now=datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc))
    decision = result["decisions"][0]
    assert decision["action"] == "execution_claimed"
    assert decision["late_by_seconds"] == 3600.0
    # A one-shot plan has no next time after its slot is consumed.
    assert ledger.get_production_task("p")["next_run_at"] is None
    assert scheduler.tick(now=datetime(2026, 9, 14, 12, 1, tzinfo=timezone.utc))["decisions"] == []


def test_manual_and_paused_plans_are_never_dispatched(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    _plan(ledger, task_id="manual-plan", due="2026-09-14T11:00:00+00:00",
          schedule={"schedule": "manual"}, ownership="manual")
    _plan(ledger, task_id="paused-plan", due="2026-09-14T11:00:00+00:00", state="paused", ownership="paused")
    result = Scheduler(ledger, instance_id="one", dispatch_enabled=True).tick(
        now=datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc))
    # A paused plan is due by the clock but the due scan only returns enabled
    # plans; the manual plan is reported as held rather than scheduled.
    assert [(item["task_id"], item["action"], item.get("reason")) for item in result["decisions"]] == [
        ("manual-plan", "hold", "manual")]
    assert ledger.list_production_executions("manual-plan") == []
    assert ledger.list_production_executions("paused-plan") == []


def test_tick_respects_its_budget(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    for index in range(5):
        _plan(ledger, task_id=f"p{index}", due="2026-09-14T11:00:00+00:00", ownership=f"k{index}")
    result = Scheduler(ledger, instance_id="one", budget=2).tick(
        now=datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc))
    assert result["evaluated"] == 2


def test_global_pause_stops_dispatch_without_resuming_on_a_heartbeat(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    _plan(ledger)
    ledger.set_global_dispatch(False, actor="system:test")
    assert ledger.dispatch_enabled() is False
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True)
    result = scheduler.tick(now=datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc))
    assert result["decisions"][0]["reason"] == "global_pause"
    assert ledger.list_production_executions("p") == []
    # A heartbeat reports liveness; it must not resume the platform by itself.
    scheduler.tick(now=datetime(2026, 9, 14, 12, 1, tzinfo=timezone.utc))
    assert ledger.dispatch_enabled() is False
    ledger.set_global_dispatch(True, actor="system:test")
    resumed = scheduler.tick(now=datetime(2026, 9, 14, 12, 2, tzinfo=timezone.utc))
    assert resumed["decisions"][0]["action"] == "execution_claimed"


def test_a_hundred_mixed_plans_keep_dispatch_bounded(tmp_path):
    """AC14: 100 mixed paused/due plans with bounded scan, paging and prefetch.

    The acceptance criterion is a shape, not a speed contest: the due scan is an
    indexed, budgeted read, the page is keyset-bounded, and one tick pre-fetches a
    bounded number of steps whatever the registry holds.
    """
    import time

    from data_center.production_tasks import ProductionTasks

    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    service = ProductionTasks(ledger)
    # The registry holds one key per instrument, so a hundred *plans* are seeded
    # through storage: this test is about the shape of the scan, not ownership.
    for index in range(100):
        task_id = f"plan-{index:03d}"
        paused = index % 2 == 0
        ledger.create_production_task(
            task_id=task_id, name=task_id, desired_state="paused" if paused else "enabled",
            provider="fixture", symbol=f"S{index:03d}",
            ownership_keys=[f"provider_bars:fixture:S{index:03d}:1m:raw"],
            next_run_at=("2026-09-14T11:59:00+00:00" if index % 4 == 1 else
                         "2027-01-01T00:00:00+00:00" if not paused else None),
            payload={"provider": "fixture", "symbol": f"S{index:03d}", "raw_timeframe": "1m",
                     "price_basis": "raw", "bar_timeframes": [],
                     "window_policy": {"mode": "continuous",
                                       "history_start": "2026-09-01T00:00:00+00:00"},
                     "schedule": {"schedule": "fixed_rate", "interval_seconds": 900,
                                  "anchor": "2026-09-14T12:00:00+00:00"}})

    budget = 10
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service,
                          budget=budget, clock=lambda: now.timestamp())
    started = time.perf_counter()
    result = scheduler.tick(now=now)
    elapsed = time.perf_counter() - started

    # Bounded work, and the whole tick stays under the acceptance budget.
    assert result["evaluated"] <= budget, result["evaluated"]
    assert elapsed < 5.0, f"tick took {elapsed:.3f}s for 100 plans"
    claimed = [item for item in result["decisions"] if item.get("action") == "execution_claimed"]
    assert claimed, "the due plans must still be dispatched under the budget"

    # Paging stays keyset-bounded and never materialises the whole registry.
    page = ledger.list_production_tasks_page(page_size=10)
    assert len(page["items"]) == 10 and page["has_more"] is True
    keys = {(item["updated_at"], item["task_id"]) for item in page["items"]}
    following = ledger.list_production_tasks_page(page_size=10, before=min(keys))
    assert following["items"] and not ({item["task_id"] for item in following["items"]}
                                       & {item["task_id"] for item in page["items"]})
