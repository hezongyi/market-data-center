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
from data_center.production_tasks import (
    ProductionTasks,
    _bucket_window,
    plan_execution,
    scheduled_end,
)
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
    service = ProductionTasks(ledger, canonical_root=tmp_path / "lake")
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


def drain(worker) -> int:
    processed = 0
    while worker.run_next():
        processed += 1
    return processed


def derived_definition(**overrides) -> dict:
    """A plan over a committed approved instrument.

    The derived step is executed by an isolated child process, which resolves
    the instrument from the committed registry; a test-only instrument is not
    visible there, so these tests use binance/BTCUSDT and publish the raw part
    themselves (no test contacts a real provider).
    """
    base = {
        "provider": "binance", "symbol": "BTCUSDT", "raw_timeframe": "1m", "price_basis": "raw",
        "bar_timeframes": ["5m"],
        "window_policy": {"mode": "continuous", "history_start": "2026-09-14T11:00:00+00:00"},
        "schedule": {"schedule": "manual"},
    }
    base.update(overrides)
    return base


def publish_raw(root, *, start, minutes=60, run_id="raw-part",
                provider="fixture", symbol="UI_TEST"):
    """Publish a valid raw 1m part for the fixture instrument.

    The fixture connector emits one bar per day whatever the timeframe, so a 1m
    window can never pass coverage checks.  The raw ingest path is covered by
    its own tests (`test_worker.py`, `test_maintenance_runner.py`); these tests
    therefore supply the published upstream part and complete the raw run
    explicitly, and exercise what they are about: the derived closure and a real
    derive execution against the fixed input.
    """
    from data_center.catalog.manifest import build_manifest, write_manifest
    from data_center.domain.models import ProviderBar
    from data_center.storage.parquet import write_provider_bars

    rows = [ProviderBar(symbol=symbol, asset_class="crypto", provider=provider, timeframe="1m",
                        bar_ts=start + timedelta(minutes=index), open=100 + index, high=102 + index,
                        low=99 + index, close=101 + index, volume=1, currency="USD",
                        price_type="raw", ingest_ts=start + timedelta(hours=1),
                        source_hash=f"raw-{index}") for index in range(minutes)]
    paths = write_provider_bars(root, rows, part_id=run_id)
    write_manifest(root, build_manifest(
        root, run_id=run_id, dataset_id="provider_bars", schema_version="provider_bars.v1",
        paths=paths, row_count=len(rows),
        quality_summary={"status": "pass", "finding_count": 0, "findings": []}))
    return paths


def complete_raw_run(ledger, root, *, start, minutes=60, provider="fixture", symbol="UI_TEST") -> None:
    """Claim the planned raw job, publish its part and record a passing receipt."""
    claim = ledger.claim_next_job()
    assert claim is not None, "the round must have queued a raw job"
    publish_raw(root, start=start, minutes=minutes, run_id=claim["run_id"],
                provider=provider, symbol=symbol)
    ledger.finish_job(claim["job_id"], claim["run_id"],
                      {"status": "pass", "run_id": claim["run_id"], "row_count": minutes})


def derived_plan(tmp_path, ledger, service, now):
    task = ledger.get_production_task("p1")
    execution = ledger.list_running_executions()[0]
    return service.plan_derived(task=task, definition=task["payload"], execution=execution,
                                step_budget=4), execution


HOURLY = "2026-09-14T11:00:00+00:00"


def test_a_plan_produces_its_derived_output_after_the_raw_publication(tmp_path):
    """raw published → 5m derived planned, executed for real, and closed (AC09)."""
    ledger, service, _ = build(tmp_path)
    service.change("p1", "update", definition=derived_definition(), expected_version=1, now=NOW)
    execution = service.change("p1", "run_now", now=NOW)
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service)
    worker = LocalWorker(tmp_path / "lake", ledger)

    scheduler.tick(now=NOW)
    raw_steps = [step for step in ledger.list_production_steps(execution["execution_id"])
                 if step["stage"] == "raw"]
    assert len(raw_steps) == 1
    complete_raw_run(ledger, tmp_path / "lake", start=datetime.fromisoformat(raw_steps[0]["window_start"]),
                     provider="binance", symbol="BTCUSDT")

    # The next tick sees the published raw window and plans the derive step.
    second = scheduler.tick(now=NOW + timedelta(minutes=1))
    assert second["reconcile"]["derived_planned"] == [
        "derive:utc-24x7-1m-to-5m-ohlcv:2026-09-14T11:00:00+00:00:2026-09-14T11:59:00+00:00"]
    derived_steps = [step for step in ledger.list_production_steps(execution["execution_id"])
                     if step["stage"] == "derive:5m"]
    assert len(derived_steps) == 1 and derived_steps[0]["recipe_id"] == "utc-24x7-1m-to-5m-ohlcv"
    # A real derive execution against the fixed input.
    assert drain(worker) == 1
    derived_runs = [run for run in ledger.list() if run.get("step_id") == derived_steps[0]["step_id"]]
    assert derived_runs[0]["status"] == "pass", derived_runs[0].get("error")
    assert derived_runs[0]["input_id"]

    third = scheduler.tick(now=NOW + timedelta(minutes=2))
    assert [item["state"] for item in third["reconcile"]["closed"]] == ["completed"]
    assert ledger.get_production_execution(execution["execution_id"])["outcome"] == "pass"


def test_derived_planning_is_deduplicated_across_ticks(tmp_path):
    ledger, service, _ = build(tmp_path)
    service.change("p1", "update", definition=derived_definition(), expected_version=1, now=NOW)
    execution = service.change("p1", "run_now", now=NOW)
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service)
    scheduler.tick(now=NOW)
    raw_steps = ledger.list_production_steps(execution["execution_id"])
    complete_raw_run(ledger, tmp_path / "lake", start=datetime.fromisoformat(raw_steps[0]["window_start"]),
                     provider="binance", symbol="BTCUSDT")
    first = scheduler.tick(now=NOW + timedelta(minutes=1))
    assert len(first["reconcile"]["derived_planned"]) == 1
    # A second closure pass over the same window plans nothing new.
    plan, _ = derived_plan(tmp_path, ledger, service, NOW)
    assert plan["steps"] == []
    assert len([step for step in ledger.list_production_steps(execution["execution_id"])
                if step["stage"] == "derive:5m"]) == 1


def test_a_derived_output_waits_until_its_window_holds_a_complete_bucket(tmp_path):
    """A one-hour window holds no complete day, so the daily hop is deferred.

    1w needs market_bars 1d, which in turn needs whole days of raw: planning it
    against a one-hour window would submit a step whose input cannot exist.
    """
    ledger, service, _ = build(tmp_path)
    service.change("p1", "update", definition=derived_definition(bar_timeframes=["1w"]),
                   expected_version=1, now=NOW)
    execution = service.change("p1", "run_now", now=NOW)
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service)
    scheduler.tick(now=NOW)
    raw_steps = ledger.list_production_steps(execution["execution_id"])
    complete_raw_run(ledger, tmp_path / "lake", start=datetime.fromisoformat(raw_steps[0]["window_start"]),
                     provider="binance", symbol="BTCUSDT")
    first, _ = derived_plan(tmp_path, ledger, service, NOW)
    assert first["steps"] == []
    # Both hops of the weekly chain are deferred: neither has a complete bucket.
    assert set(first["deferred"]) == {
        "derive:utc-24x7-1m-to-1d-ohlcv:2026-09-14T11:00:00+00:00:2026-09-14T11:59:00+00:00",
        "derive:utc-24x7-1d-to-1w-ohlcv:2026-09-14T11:00:00+00:00:2026-09-14T11:59:00+00:00"}
    # A wider window that does hold complete days can plan the daily hop.
    assert _bucket_window("2026-09-08T00:00:00Z", "2026-09-14T11:59:00Z", "1d") == (
        "2026-09-08T00:00:00+00:00", "2026-09-14T00:00:00+00:00")
