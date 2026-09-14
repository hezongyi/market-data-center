"""End-to-end dispatch: a plan becomes runs, the worker executes them, the round closes.

This is the acceptance path that matters for S3: nothing here is mocked at the
ledger boundary.  A real `LocalWorker` claims the jobs the scheduler enqueued,
publishes through the normal staging path, and the next tick closes the
execution from the run outcomes it can read back (AC05, AC06, AC09, AC13).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from data_center.control_plane import InstrumentMetadata
from data_center.ingest.worker import LocalWorker
from data_center.platform_registry import REGISTRY
from data_center.production_tasks import ProductionTasks, plan_execution, scheduled_end
from data_center.runs.ledger import RunLedger
from data_center.scheduler import Scheduler

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fixture_instrument():
    """The fixture provider is the documented isolated acceptance path."""
    if not any(item.symbol == "UI_TEST" for item in REGISTRY.instruments("fixture", approved_only=False)):
        REGISTRY.register_instrument(InstrumentMetadata(
            provider="fixture", symbol="UI_TEST", canonical_symbol="UI_TEST", asset_class="crypto",
            currency="USD", session_profile="utc_24x7", calendar_profile="crypto_24x7_v1"))


def definition(**overrides) -> dict:
    # The fixture connector emits one bar per day, so the isolated acceptance
    # path plans daily windows; the production 1m path is exercised by the
    # governed Dukascopy provider, not by a synthetic one.
    base = {
        "provider": "fixture", "symbol": "UI_TEST", "raw_timeframe": "1d", "price_basis": "raw",
        "bar_timeframes": [],
        "window_policy": {"mode": "continuous", "history_start": "2026-09-08T00:00:00+00:00"},
        "schedule": {"schedule": "manual"},
    }
    base.update(overrides)
    return base


def build(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    service = ProductionTasks(ledger)
    task = service.create(definition=definition(), name="UI_TEST", task_id="p1",
                          desired_state="enabled", now=NOW)
    return ledger, service, task


def test_manual_run_now_is_planned_and_executed_by_a_real_worker(tmp_path):
    ledger, service, _ = build(tmp_path)
    execution = service.change("p1", "run_now", now=NOW)
    assert ledger.list_production_steps(execution["execution_id"]) == []

    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service)
    first = scheduler.tick(now=NOW)
    decision = next(item for item in first["decisions"] if item.get("execution_id") == execution["execution_id"])
    assert decision["action"] == "execution_claimed"
    assert decision["steps"] >= 1 and decision["runs"] >= 1

    # The run is attributed to its plan, execution and step, not just to a job.
    steps = ledger.list_production_steps(execution["execution_id"])
    assert [step["stage"] for step in steps] == ["raw"]
    runs = [run for run in ledger.list() if run.get("execution_id") == execution["execution_id"]]
    assert len(runs) == decision["runs"]
    assert all(run["plan_id"] == "p1" and run["step_id"] == steps[0]["step_id"] for run in runs)
    assert ledger.get_production_execution(execution["execution_id"])["state"] == "running"

    # A real worker claims the jobs and publishes through the normal path.
    worker = LocalWorker(tmp_path / "lake", ledger)
    processed = 0
    while worker.run_next():
        processed += 1
    assert processed == decision["runs"]
    assert all(ledger.get(run["run_id"])["status"] == "pass" for run in runs)

    # The next tick closes the round from the run outcomes it read back.
    second = scheduler.tick(now=NOW + timedelta(minutes=1))
    assert second["reconcile"]["closed"][0]["state"] == "completed"
    assert ledger.get_production_execution(execution["execution_id"])["outcome"] == "pass"
    assert ledger.list_production_steps(execution["execution_id"])[0]["state"] == "completed"
    progress = ledger.production_progress("p1")
    assert progress["last_outcome"] == "pass" and progress["frontier"]


def test_a_failed_run_makes_the_round_failed_and_keeps_the_receipt(tmp_path):
    ledger, service, _ = build(tmp_path)
    execution = service.change("p1", "run_now", now=NOW)
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service)
    scheduler.tick(now=NOW)
    run = next(item for item in ledger.list() if item.get("execution_id") == execution["execution_id"])
    claim = ledger.claim_next_job()
    ledger.fail_job(claim["job_id"], run["run_id"], "provider exploded",
                    error_type="ProviderError", retryable=False)

    scheduler.tick(now=NOW + timedelta(minutes=1))
    closed = ledger.get_production_execution(execution["execution_id"])
    assert closed["state"] == "failed" and closed["outcome"] == "failed"
    # The run keeps its own terminal receipt; closure never rewrites it.
    assert ledger.get(run["run_id"])["status"] == "failed"
    assert ledger.list_production_steps(execution["execution_id"])[0]["state"] == "failed"


def test_pause_lets_the_in_flight_round_finish_then_stops(tmp_path):
    ledger, service, _ = build(tmp_path)
    execution = service.change("p1", "run_now", now=NOW)
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service)
    scheduler.tick(now=NOW)
    service.change("p1", "pause", now=NOW)
    # The accepted round is marked pausing, not abandoned.
    assert ledger.get_production_execution(execution["execution_id"])["state"] == "pausing"
    # Its queued jobs are no longer claimable while the plan is paused.
    assert ledger.claim_next_job() is None
    service.change("p1", "resume", now=NOW)
    assert ledger.get_production_execution(execution["execution_id"])["state"] == "running"
    assert ledger.claim_next_job() is not None


def test_paused_plan_keeps_its_jobs_for_after_the_resume(tmp_path):
    ledger, service, _ = build(tmp_path)
    execution = service.change("p1", "run_now", now=NOW)
    Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service).tick(now=NOW)
    queued = [run for run in ledger.list() if run.get("execution_id") == execution["execution_id"]]
    service.change("p1", "pause", now=NOW)
    # A pause is not a failure: the jobs stay queued, unclaimed and un-attempted.
    assert ledger.job_queue_state()["by_status"]["queued"] == len(queued)
    assert all(run.get("attempt_count") is None for run in ledger.list()
               if run.get("execution_id") == execution["execution_id"])
    service.change("p1", "resume", now=NOW)
    assert len([run for run in ledger.list()
                if run.get("execution_id") == execution["execution_id"]]) == len(queued)


def test_planning_is_bounded_by_policy_and_reports_a_backlog(tmp_path):
    ledger, _service, _ = build(tmp_path)
    task = ledger.get_production_task("p1")
    # Eight months of history cannot be expanded in one round at 31 days max.
    task["payload"]["window_policy"]["history_start"] = "2026-03-01T00:00:00+00:00"
    plan = plan_execution(task, task["payload"], {"execution_id": "e" * 8}, now=NOW, step_budget=4)
    assert len(plan["steps"]) == 4
    assert plan["backlog"] is True
    assert plan["truncated_windows"] > 0
    # The bounded round never plans past its policy window.
    planned_end = datetime.fromisoformat(plan["planned_end"])
    assert planned_end <= datetime.fromisoformat(plan["planned_start"]) + timedelta(days=31)


def test_scheduled_end_respects_the_provider_availability_lag():
    assert scheduled_end(NOW, lag_minutes=180) == NOW - timedelta(hours=3)
    # The lag is subtracted first, then the boundary is closed to the minute.
    assert scheduled_end(NOW.replace(second=42), lag_minutes=1) == NOW.replace(
        second=0, microsecond=0) - timedelta(minutes=1)


def test_fixed_delay_advances_only_after_the_round_ends(tmp_path):
    ledger, service, _ = build(tmp_path)
    definition_doc = definition(schedule={"schedule": "fixed_delay", "interval_seconds": 900})
    ledger.update_production_task("p1", {**definition_doc, "window_policy": definition_doc["window_policy"]},
                                  expected_version=1)
    execution = service.change("p1", "run_now", now=NOW)
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service)
    scheduler.tick(now=NOW)
    # No absolute next time exists until the round terminates.
    assert ledger.get_production_task("p1")["next_run_at"] is None
    worker = LocalWorker(tmp_path / "lake", ledger)
    while worker.run_next():
        pass
    scheduler.tick(now=NOW + timedelta(minutes=1))
    finished_at = datetime.fromisoformat(ledger.get_production_execution(execution["execution_id"])["finished_at"])
    assert ledger.get_production_task("p1")["next_run_at"] == (finished_at + timedelta(seconds=900)).isoformat()


def test_two_schedulers_racing_plan_a_slot_once(tmp_path):
    ledger, service, _ = build(tmp_path)
    ledger.update_production_task("p1", definition(
        schedule={"schedule": "fixed_rate", "interval_seconds": 900,
                  "anchor": "2026-09-14T11:59:00+00:00"}), expected_version=1)
    ledger.set_task_next_run_at(task_id="p1", next_run_at="2026-09-14T11:59:00+00:00")
    first = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service)
    second = Scheduler(ledger, instance_id="two", dispatch_enabled=True, planner=service)
    results = [first.tick(now=NOW), second.tick(now=NOW)]
    claimed = [item for result in results for item in result["decisions"]
               if item.get("action") == "execution_claimed"]
    assert len(claimed) == 1
    assert len(ledger.list_production_executions("p1")) == 1
    assert len([run for run in ledger.list() if run.get("plan_id") == "p1"]) == claimed[0]["runs"]
