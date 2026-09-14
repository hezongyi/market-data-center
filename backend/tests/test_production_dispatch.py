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
from data_center.instants import parse_instant
from data_center.platform_registry import REGISTRY
from data_center.production_tasks import (
    ProductionTasks,
    _bucket_window,
    plan_execution,
    scheduled_end,
)
from data_center.runs.ledger import ProductionConflict, RunLedger
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
    # A plan splits its first round into the observed tail and the older backlog
    # (spec 5.5), so the round may hold more than one raw window.
    steps = ledger.list_production_steps(execution["execution_id"])
    assert {step["stage"] for step in steps} == {"raw"} and len(steps) >= 1
    runs = [run for run in ledger.list() if run.get("execution_id") == execution["execution_id"]]
    assert len(runs) == decision["runs"]
    assert all(run["plan_id"] == "p1" and run["step_id"] in {step["step_id"] for step in steps}
               for run in runs)
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
    claim = ledger.claim_next_job()
    failed = ledger.get(claim["run_id"])
    ledger.fail_job(claim["job_id"], claim["run_id"], "provider exploded",
                    error_type="ProviderError", retryable=False)
    # The round may hold more than one raw window; the others have to reach a
    # terminal state before the round itself can be read back as failed.
    while (rest := ledger.claim_next_job()) is not None:
        ledger.finish_job(rest["job_id"], rest["run_id"],
                          {"status": "pass", "run_id": rest["run_id"]})

    scheduler.tick(now=NOW + timedelta(minutes=1))
    closed = ledger.get_production_execution(execution["execution_id"])
    assert closed["state"] == "failed" and closed["outcome"] == "failed"
    # The run keeps its own terminal receipt; closure never rewrites it.
    assert ledger.get(failed["run_id"])["status"] == "failed"
    failed_step = next(step for step in ledger.list_production_steps(execution["execution_id"])
                       if step["step_id"] == failed["step_id"])
    assert failed_step["state"] == "failed"


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
    planned_end = parse_instant(plan["planned_end"])
    assert planned_end <= parse_instant(plan["planned_start"]) + timedelta(days=31)


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
    finished_at = parse_instant(ledger.get_production_execution(execution["execution_id"])["finished_at"])
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


def two_symbol_definition(provider: str, symbol: str, price_basis: str,
                          history_start: str, **overrides) -> dict:
    """A 24x7 plan per symbol: two approved instruments, two ownership keys.

    The providers carry different availability lags, so each plan gets the
    history start that makes its own effective window plannable.
    """
    base = {
        "provider": provider, "symbol": symbol, "raw_timeframe": "1m", "price_basis": price_basis,
        "bar_timeframes": ["5m"],
        "window_policy": {"mode": "continuous", "history_start": history_start},
        "schedule": {"schedule": "manual"},
    }
    base.update(overrides)
    return base


def test_one_symbols_gap_does_not_block_another_symbol(tmp_path):
    """AC12: a gap blocks the buckets covering it, and other symbols proceed."""
    from data_center.platform_registry import REGISTRY
    from data_center.storage.query import query_market_bars

    ledger, service, _ = build(tmp_path)
    recipe = REGISTRY.recipe("utc-24x7-1m-to-5m-ohlcv", "1")
    # ``build`` already owns p1, so the two symbols get their own plans.  Both
    # sessions are continuous, so only the providers' availability lags differ.
    plans = {
        "p2": ("binance", "BTCUSDT", "raw", "2026-09-14T11:00:00+00:00"),
        "p3": ("dukascopy", "BTCUSD", "bid", "2026-09-14T08:00:00+00:00"),
    }
    for task_id, (provider, symbol, basis, history_start) in plans.items():
        service.create(definition=two_symbol_definition(provider, symbol, basis, history_start),
                       name=task_id, task_id=task_id, desired_state="enabled", now=NOW)
    executions = {task_id: service.change(task_id, "run_now", now=NOW) for task_id in plans}
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service)
    worker = LocalWorker(tmp_path / "lake", ledger)
    scheduler.tick(now=NOW)

    # Every raw window of both plans is published; one symbol's window is missing
    # a single minute inside its 11:30-11:35 bucket.
    for _ in range(2):
        claim = ledger.claim_next_job()
        run = ledger.get(claim["run_id"])
        step = next(item for item in ledger.list_production_steps(run["execution_id"])
                    if item["step_id"] == run["step_id"])
        start = parse_instant(step["window_start"])
        publish_raw(tmp_path / "lake", start=start, minutes=60, run_id=claim["run_id"],
                    provider=run["provider"], symbol=run["symbol"],
                    skip_minute=32 if run["plan_id"] == "p2" else None)
        ledger.finish_job(claim["job_id"], claim["run_id"],
                          {"status": "pass", "run_id": claim["run_id"], "row_count": 60})

    scheduler.tick(now=NOW + timedelta(minutes=1))
    # The blocked symbol owes exactly the bucket the gap covers; the other symbol
    # owes nothing at all and has its whole window planned.
    owed = service.read("p2")["progress"]["deferred_derived"]
    assert [item.split("derive:")[-1].split(":", 1)[1] for item in owed] == [
        "2026-09-14T11:30:00+00:00:2026-09-14T11:35:00+00:00"]
    assert service.read("p3")["progress"]["deferred_derived"] == []
    assert [step for step in ledger.list_production_steps(executions["p3"]["execution_id"])
            if step["stage"] == "derive:5m"]

    # The real worker derives both symbols: each publishes every complete bucket,
    # and only the gap-covered bucket of the first symbol is missing.
    assert drain(worker) >= 3
    def buckets(provider: str, symbol: str, basis: str) -> list[str]:
        return [row["bar_ts"].isoformat() for row in query_market_bars(
            tmp_path / "lake", provider=provider, symbol=symbol, timeframe="5m",
            price_basis=basis, recipe_id=recipe.recipe_id, recipe_version=recipe.version)]

    # The published rows carry the synthetic part's basis, so both symbols are
    # read back by it; isolation is what this test is about, not basis naming.
    blocked_symbol = buckets("binance", "BTCUSDT", "raw")
    other_symbol = buckets("dukascopy", "BTCUSD", "raw")
    assert len(blocked_symbol) == 10 and "2026-09-14T11:30:00+00:00" not in blocked_symbol
    assert len(other_symbol) == 12, other_symbol
    assert other_symbol == [f"2026-09-14T{hour:02d}:{minute:02d}:00+00:00"
                            for hour in (8,) for minute in range(0, 60, 5)]


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
                provider="fixture", symbol="UI_TEST", skip_minute: int | None = None):
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
                        source_hash=f"raw-{index}") for index in range(minutes)
            if skip_minute is None or index != skip_minute]
    paths = write_provider_bars(root, rows, part_id=run_id)
    write_manifest(root, build_manifest(
        root, run_id=run_id, dataset_id="provider_bars", schema_version="provider_bars.v1",
        paths=paths, row_count=len(rows),
        quality_summary={"status": "pass", "finding_count": 0, "findings": []}))
    return paths


def test_a_hole_blocks_only_the_bucket_that_covers_it(tmp_path):
    """AC12: one missing bar blocks its own bucket, not the whole window.

    The complete buckets of the same window are derived and published, the
    bucket covering the hole stays visible as deferred, and repairing the hole
    re-derives exactly that bucket instead of the whole window.
    """
    from data_center.platform_registry import REGISTRY
    from data_center.storage.query import query_market_bars

    ledger, service, _ = build(tmp_path)
    service.change("p1", "update", definition=derived_definition(), expected_version=1, now=NOW)
    execution = service.change("p1", "run_now", now=NOW)
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service)
    worker = LocalWorker(tmp_path / "lake", ledger)
    recipe = REGISTRY.recipe("utc-24x7-1m-to-5m-ohlcv", "1")

    # The raw window is published with a hole inside its 11:30-11:35 bucket.
    scheduler.tick(now=NOW)
    raw = ledger.list_production_steps(execution["execution_id"])[0]
    complete_raw_run(ledger, tmp_path / "lake", start=parse_instant(raw["window_start"]),
                     provider="binance", symbol="BTCUSDT", skip_minute=32)

    second = scheduler.tick(now=NOW + timedelta(minutes=1))
    derived = sorted((step for step in ledger.list_production_steps(execution["execution_id"])
                      if step["stage"] == "derive:5m"),
                     key=lambda step: step["window_start"])
    assert [(step["window_start"], step["window_end"]) for step in derived] == [
        ("2026-09-14T11:00:00+00:00", "2026-09-14T11:30:00+00:00"),
        ("2026-09-14T11:35:00+00:00", "2026-09-14T11:55:00+00:00")]
    # The blocked bucket is reported on the plan, not silently dropped.
    blocked = "derive:utc-24x7-1m-to-5m-ohlcv:2026-09-14T11:30:00+00:00:2026-09-14T11:35:00+00:00"
    assert service.read("p1")["progress"]["deferred_derived"] == [blocked]
    assert blocked in second["reconcile"]["derived_deferred"]

    # The complete buckets are derived and published by the real worker.
    assert drain(worker) == 2
    assert len(query_market_bars(tmp_path / "lake", provider="binance", symbol="BTCUSDT",
                                 timeframe="5m", price_basis="raw", recipe_id=recipe.recipe_id,
                                 recipe_version=recipe.version)) == 10

    # A second round repairs the hole with its own gap-repair window (and fetches
    # the tail that has become due); every window of the round is published.
    third = scheduler.tick(now=NOW + timedelta(minutes=2))
    assert ledger.get_production_execution(execution["execution_id"])["outcome"] == "pass"
    repair_round = service.change("p1", "run_now", now=NOW + timedelta(minutes=2))
    scheduler.tick(now=NOW + timedelta(minutes=2))
    gap_windows = sorted((step["window_start"], step["window_end"])
                         for step in ledger.list_production_steps(repair_round["execution_id"])
                         if step["stage"] == "raw")
    assert ("2026-09-14T11:32:00+00:00", "2026-09-14T11:33:00+00:00") in gap_windows
    while (claim := ledger.claim_next_job()) is not None:
        claimed = ledger.get(claim["run_id"])
        step = next(item for item in ledger.list_production_steps(repair_round["execution_id"])
                    if item["step_id"] == claimed["step_id"])
        assert claimed["execution_plan"]["windows"][0]["reason"] in {"gap_repair", "tail"}
        publish_raw(tmp_path / "lake", start=parse_instant(step["window_start"]),
                    minutes=1, run_id=claim["run_id"], provider="binance", symbol="BTCUSDT")
        ledger.finish_job(claim["job_id"], claim["run_id"],
                          {"status": "pass", "run_id": claim["run_id"], "row_count": 1})

    # The closure plans exactly the buckets that were missing: the blocked one and
    # the one the repaired bar made complete, never the buckets already derived.
    scheduler.tick(now=NOW + timedelta(minutes=3))
    repaired = sorted((step["window_start"], step["window_end"])
                       for step in ledger.list_production_steps(repair_round["execution_id"])
                       if step["stage"] == "derive:5m")
    assert repaired == [("2026-09-14T11:30:00+00:00", "2026-09-14T11:35:00+00:00"),
                        ("2026-09-14T11:55:00+00:00", "2026-09-14T12:00:00+00:00")]
    assert drain(worker) == 2

    # The repaired bucket is published, the others were not derived twice, and the
    # plan no longer reports that bucket as waiting.
    rows = query_market_bars(tmp_path / "lake", provider="binance", symbol="BTCUSDT",
                             timeframe="5m", price_basis="raw", recipe_id=recipe.recipe_id,
                             recipe_version=recipe.version)
    assert [row["bar_ts"].isoformat() for row in rows] == [
        "2026-09-14T11:00:00+00:00", "2026-09-14T11:05:00+00:00", "2026-09-14T11:10:00+00:00",
        "2026-09-14T11:15:00+00:00", "2026-09-14T11:20:00+00:00", "2026-09-14T11:25:00+00:00",
        "2026-09-14T11:30:00+00:00", "2026-09-14T11:35:00+00:00", "2026-09-14T11:40:00+00:00",
        "2026-09-14T11:45:00+00:00", "2026-09-14T11:50:00+00:00", "2026-09-14T11:55:00+00:00"]
    assert blocked not in service.read("p1")["progress"]["deferred_derived"]
    del third


def complete_raw_run(ledger, root, *, start, minutes=60, provider="fixture", symbol="UI_TEST",
                     skip_minute: int | None = None) -> None:
    """Claim the planned raw job, publish its part and record a passing receipt."""
    claim = ledger.claim_next_job()
    assert claim is not None, "the round must have queued a raw job"
    publish_raw(root, start=start, minutes=minutes, run_id=claim["run_id"],
                provider=provider, symbol=symbol, skip_minute=skip_minute)
    ledger.finish_job(claim["job_id"], claim["run_id"],
                      {"status": "pass", "run_id": claim["run_id"],
                       "row_count": minutes - (1 if skip_minute is not None else 0)})


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
    complete_raw_run(ledger, tmp_path / "lake", start=parse_instant(raw_steps[0]["window_start"]),
                     provider="binance", symbol="BTCUSDT")

    # The next tick sees the published raw window and plans the derive step.
    second = scheduler.tick(now=NOW + timedelta(minutes=1))
    # The identity is the bucket-aligned run that will actually be derived, so
    # the same bucket can never be derived twice under two raw-window names.
    assert second["reconcile"]["derived_planned"] == [
        "derive:utc-24x7-1m-to-5m-ohlcv:2026-09-14T11:00:00+00:00:2026-09-14T11:55:00+00:00"]
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
    complete_raw_run(ledger, tmp_path / "lake", start=parse_instant(raw_steps[0]["window_start"]),
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
    complete_raw_run(ledger, tmp_path / "lake", start=parse_instant(raw_steps[0]["window_start"]),
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


def test_reconciliation_derives_what_a_crashed_round_never_planned(tmp_path):
    """AC10: raw published, process died, the next tick still produces the derived output.

    The round is closed without its derived layer ever being planned, exactly as
    a crash between publication and planning would leave it.
    """
    ledger, service, _ = build(tmp_path)
    service.change("p1", "update", definition=derived_definition(), expected_version=1, now=NOW)
    execution = service.change("p1", "run_now", now=NOW)
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service)
    scheduler.tick(now=NOW)
    raw_steps = ledger.list_production_steps(execution["execution_id"])
    complete_raw_run(ledger, tmp_path / "lake", start=parse_instant(raw_steps[0]["window_start"]),
                     provider="binance", symbol="BTCUSDT")
    # The round terminates with no derived plan at all.
    ledger.refresh_execution_steps(execution["execution_id"])
    assert ledger.close_finished_executions()
    assert ledger.get_production_execution(execution["execution_id"])["state"] == "completed"
    assert ledger.list_production_steps(execution["execution_id"])[0]["stage"] == "raw"

    # Restart: a fresh scheduler reconciles the publication cursor and plans the
    # derived step into a round of its own.
    # The restarted process keeps its instance identity, so it renews its own
    # lease instead of waiting for the previous one to expire.
    restarted = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service)
    result = restarted.tick(now=NOW + timedelta(minutes=5))
    # The identity names the whole 5m buckets the work will produce.
    assert result["reconcile"]["derived_planned"] == [
        "derive:utc-24x7-1m-to-5m-ohlcv:2026-09-14T11:00:00+00:00:2026-09-14T11:55:00+00:00"]
    reconciled = ledger.list_production_executions("p1")
    derived = [step for step in ledger.list_production_steps(reconciled[0]["execution_id"])
               if step["stage"] == "derive:5m"]
    assert len(derived) == 1
    assert reconciled[0]["trigger_source"] == "reconcile"
    assert service.read("p1")["current_execution"]["execution_id"] == reconciled[0]["execution_id"]


def test_the_derivation_cursor_stops_the_work_being_planned_twice(tmp_path):
    ledger, service, _ = build(tmp_path)
    service.change("p1", "update", definition=derived_definition(), expected_version=1, now=NOW)
    execution = service.change("p1", "run_now", now=NOW)
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service)
    scheduler.tick(now=NOW)
    raw_steps = ledger.list_production_steps(execution["execution_id"])
    complete_raw_run(ledger, tmp_path / "lake", start=parse_instant(raw_steps[0]["window_start"]),
                     provider="binance", symbol="BTCUSDT")
    first = service.reconcile_publications(limit=5, step_budget=4)
    assert len(first["planned"]) == 1
    assert ledger.production_progress("p1")["derived_cursor"] == "2026-09-14T11:55:00+00:00"
    # Nothing new was published, so no new step is planned.  The residue past
    # the last complete bucket stays deferred rather than being dropped or
    # planned against a partial bucket.
    second = service.reconcile_publications(limit=5, step_budget=4)
    assert second["planned"] == []
    assert second["deferred"] == [
        "derive:utc-24x7-1m-to-5m-ohlcv:2026-09-14T11:55:00+00:00:2026-09-14T11:59:00+00:00"]
    assert len([step for step in ledger.list_production_steps(
        ledger.active_execution_for_task("p1")["execution_id"]) if step["stage"] == "derive:5m"]) == 1


def test_raw_progress_without_a_derivation_is_repaired_without_new_raw(tmp_path):
    """AC10: raw needs no update, but a missing derived output is still produced."""
    ledger, service, _ = build(tmp_path)
    service.change("p1", "update", definition=derived_definition(), expected_version=1, now=NOW)
    execution = service.change("p1", "run_now", now=NOW)
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service)
    scheduler.tick(now=NOW)
    raw_steps = ledger.list_production_steps(execution["execution_id"])
    complete_raw_run(ledger, tmp_path / "lake", start=parse_instant(raw_steps[0]["window_start"]),
                     provider="binance", symbol="BTCUSDT")
    # Two ticks with no new raw: the second must not duplicate the derived work.
    scheduler.tick(now=NOW + timedelta(minutes=1))
    before = len(ledger.list())
    second = scheduler.tick(now=NOW + timedelta(minutes=2))
    assert second["reconcile"]["derived_planned"] == []
    assert len(ledger.list()) == before


def _failed_round(tmp_path, service, ledger, execution):
    """Drive a round to a terminal failed state with one failed raw run."""
    claim = ledger.claim_next_job()
    assert claim is not None
    ledger.fail_job(claim["job_id"], claim["run_id"], "provider exploded",
                    error_type="ProviderError", retryable=False)
    ledger.refresh_execution_steps(execution["execution_id"])
    ledger.close_finished_executions()
    return ledger.get_production_execution(execution["execution_id"])


def test_retry_replans_only_the_unfinished_windows_as_a_linked_round(tmp_path):
    ledger, service, _ = build(tmp_path)
    service.change("p1", "update", definition=derived_definition(), expected_version=1, now=NOW)
    execution = service.change("p1", "run_now", now=NOW)
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service)
    scheduler.tick(now=NOW)
    original = _failed_round(tmp_path, service, ledger, execution)
    assert original["state"] == "failed"
    original_receipt = ledger.get(ledger.list()[0]["run_id"])

    retried = service.retry(execution_id=execution["execution_id"], now=NOW + timedelta(minutes=1))
    assert retried["retry_of_execution_id"] == execution["execution_id"]
    follow_up = ledger.get_production_execution(retried["execution_id"])
    # The follow-up round is created together with its first plan, so it is
    # already running its planned steps.
    assert follow_up["trigger_source"] == "retry" and follow_up["state"] == "running"
    assert follow_up["retry_of_execution_id"] == execution["execution_id"]
    # Only the failed window is re-planned, and it carries its own fixed input.
    assert retried["planned_steps"] == 1 and retried["run_ids"]
    # The original round's terminal result is untouched.
    assert ledger.get_production_execution(execution["execution_id"])["state"] == "failed"
    assert ledger.get(original_receipt["run_id"]) == original_receipt
    # The follow-up round re-plans the raw window; complete it the same isolated
    # way as the first round so no test contacts a real provider.
    follow_up_raw = ledger.list_production_steps(retried["execution_id"])[0]
    claim = ledger.claim_next_job()
    assert claim["run_id"] in retried["run_ids"]
    publish_raw(tmp_path / "lake", start=parse_instant(follow_up_raw["window_start"]),
                run_id=claim["run_id"], provider="binance", symbol="BTCUSDT")
    ledger.finish_job(claim["job_id"], claim["run_id"],
                      {"status": "pass", "run_id": claim["run_id"], "row_count": 60})
    scheduler.tick(now=NOW + timedelta(minutes=2))
    # The follow-up round advances its own dependency graph: the raw window it
    # re-planned is complete and its derived layer has been planned.
    follow_up_steps = ledger.list_production_steps(retried["execution_id"])
    assert any(step["state"] == "completed" for step in follow_up_steps)
    assert any(step["stage"].startswith("derive:") for step in follow_up_steps)


def test_retry_refuses_a_running_round_and_a_paused_plan(tmp_path):
    ledger, service, _ = build(tmp_path)
    service.change("p1", "update", definition=derived_definition(), expected_version=1, now=NOW)
    execution = service.change("p1", "run_now", now=NOW)
    Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service).tick(now=NOW)
    # Still running: a retry would race the round in flight.
    with pytest.raises(ProductionConflict) as running:
        service.retry(execution_id=execution["execution_id"], now=NOW)
    assert running.value.code == "active_execution"
    _failed_round(tmp_path, service, ledger, execution)
    service.change("p1", "pause", now=NOW)
    with pytest.raises(ProductionConflict) as paused:
        service.retry(execution_id=execution["execution_id"], now=NOW)
    assert paused.value.code == "task_paused"
    # Nothing was created by either refusal.
    assert len(ledger.list_production_executions("p1")) == 1


def test_retry_reports_an_unknown_round_and_audits_the_refusal(tmp_path):
    ledger, service, _ = build(tmp_path)
    with pytest.raises(KeyError):
        service.retry(execution_id="absent")
    entries = ledger.write_audit_entries()
    assert entries[0]["action"] == "production.execution.retry"
    assert entries[0]["outcome"] == "rejected" and entries[0]["code"] == "not_found"


def test_retry_is_idempotent_under_a_repeated_key(tmp_path):
    ledger, service, _ = build(tmp_path)
    service.change("p1", "update", definition=derived_definition(), expected_version=1, now=NOW)
    execution = service.change("p1", "run_now", now=NOW)
    Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service).tick(now=NOW)
    _failed_round(tmp_path, service, ledger, execution)
    first = service.retry(execution_id=execution["execution_id"], idempotency_key="retry-1", now=NOW)
    replay = service.retry(execution_id=execution["execution_id"], idempotency_key="retry-1", now=NOW)
    assert replay == first
    assert len(ledger.list_production_executions("p1")) == 2


def test_a_republished_raw_window_sends_its_derived_output_back_for_recompute(tmp_path):
    """AC12: repairing raw must re-derive the outputs that already existed.

    The first round derives 5m from a raw window.  That window is then published
    again (a repair), and the derived output no longer reflects its input, so the
    range must be re-derived even though the cursor has already passed it.
    """
    ledger, service, _ = build(tmp_path)
    service.change("p1", "update", definition=derived_definition(), expected_version=1, now=NOW)
    execution = service.change("p1", "run_now", now=NOW)
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service)
    worker = LocalWorker(tmp_path / "lake", ledger)

    # Round one: raw published, 5m derived and completed.
    scheduler.tick(now=NOW)
    raw = ledger.list_production_steps(execution["execution_id"])[0]
    complete_raw_run(ledger, tmp_path / "lake", start=parse_instant(raw["window_start"]),
                     provider="binance", symbol="BTCUSDT")
    scheduler.tick(now=NOW + timedelta(minutes=1))
    assert drain(worker) == 1
    scheduler.tick(now=NOW + timedelta(minutes=2))
    assert ledger.get_production_execution(execution["execution_id"])["outcome"] == "pass"
    assert service.recompute_pending("p1") == []

    # A repair round republishes the same raw window: rewinding the recorded
    # frontier is how a manual gap repair over an older range is requested, and
    # it must still be honored even though coverage has no complaint about it.
    ledger.record_progress("p1", {"frontier": raw["window_start"]})
    repair = service.change("p1", "run_now", now=NOW + timedelta(minutes=3))
    scheduler.tick(now=NOW + timedelta(minutes=3))
    # The round plans the observed tail and the rewound range, so every raw
    # window of the round has to finish before the plan's closure reads it back.
    # Each claimed window is published for the window its own step asked for.
    claimed_runs = []
    while (claim := ledger.claim_next_job()) is not None:
        claimed = ledger.get(claim["run_id"])
        claimed_runs.append(claim["run_id"])
        repair_raw = next(step for step in ledger.list_production_steps(repair["execution_id"])
                          if step["step_id"] == claimed["step_id"])
        publish_raw(tmp_path / "lake", start=parse_instant(repair_raw["window_start"]),
                    run_id=claim["run_id"], provider="binance", symbol="BTCUSDT")
        ledger.finish_job(claim["job_id"], claim["run_id"],
                          {"status": "pass", "run_id": claim["run_id"], "row_count": 60})
    assert claimed_runs and all(
        run_id in [run["run_id"] for run in ledger.list()
                   if run.get("execution_id") == repair["execution_id"]]
        for run_id in claimed_runs)

    # The closure phase records the repaired window as owed...
    service._advance_dependency_closure(limit=5, step_budget=4)
    owed = service.recompute_pending("p1")
    assert [item["reason"] for item in owed] == ["raw_republished_after_derivation"]
    # It starts and ends where the repaired window did, so the derived range
    # that is now stale is covered end to end.
    assert owed[0]["window_start"] == raw["window_start"]
    assert owed[0]["window_end"] >= raw["window_end"]

    # ...and the publication phase re-derives it and clears the debt, so the
    # repaired range is produced exactly once instead of every tick.
    service.reconcile_publications(limit=5, step_budget=4)
    assert service.recompute_pending("p1") == []
    recomputed_steps = [step for step in ledger.list_production_steps(repair["execution_id"])
                        if step["stage"].startswith("derive:")]
    covering = [step for step in recomputed_steps
                if step["window_start"] <= raw["window_start"]
                and step["window_end"] >= raw["window_start"]]
    assert covering, f"the repaired range must be re-derived: {recomputed_steps}"


def test_a_recompute_that_cannot_be_derived_stays_owed_with_its_attempt_count(tmp_path):
    _ledger, service, _ = build(tmp_path)
    service.change("p1", "update", definition=derived_definition(), expected_version=1, now=NOW)
    service.change("p1", "run_now", now=NOW)
    service.record_recompute("p1", window_start="2026-09-14T11:00:00+00:00",
                            window_end="2026-09-14T11:05:00+00:00", reason="raw_republished_after_derivation")
    # Nothing is published for that selector yet, so the range stays owed and
    # records that it was attempted rather than being dropped.
    result = service.reconcile_publications(limit=5, step_budget=4)
    assert result["planned"] == []
    owed = service.recompute_pending("p1")
    assert len(owed) == 1 and owed[0]["attempts"] == 1


def test_raw_published_outside_the_plan_owes_its_derived_windows(tmp_path):
    """AC10/AC12: a manual repair by another entry point must be noticed.

    The plan only ever sees the published catalog, so it remembers which raw
    parts it has already accounted for and owes the derivation of any window a
    repaired part invalidates.
    """
    ledger, service, _ = build(tmp_path)
    service.change("p1", "update", definition=derived_definition(), expected_version=1, now=NOW)
    execution = service.change("p1", "run_now", now=NOW)
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service)
    worker = LocalWorker(tmp_path / "lake", ledger)

    scheduler.tick(now=NOW)
    raw = ledger.list_production_steps(execution["execution_id"])[0]
    complete_raw_run(ledger, tmp_path / "lake", start=parse_instant(raw["window_start"]),
                     provider="binance", symbol="BTCUSDT")
    scheduler.tick(now=NOW + timedelta(minutes=1))
    assert drain(worker) == 1
    scheduler.tick(now=NOW + timedelta(minutes=2))
    assert ledger.get_production_execution(execution["execution_id"])["outcome"] == "pass"
    # The plan has now seen the raw layer once: that observation is a baseline.
    assert service.recompute_pending("p1") == []

    # Another governed entry point publishes a repaired part for the same window,
    # recorded in the ledger as its own run.
    repaired_run = ledger.enqueue_job({"job_id": "external-repair", "dataset_id": "provider_bars",
                                       "provider": "binance", "symbol": "BTCUSDT", "timeframe": "1m",
                                       "start": raw["window_start"], "end": raw["window_end"],
                                       "run_kind": "gap_repair", "run_scope": "maintenance"})
    claim = ledger.claim_next_job()
    assert claim["run_id"] == repaired_run
    publish_raw(tmp_path / "lake", start=parse_instant(raw["window_start"]),
                run_id=repaired_run, provider="binance", symbol="BTCUSDT")
    ledger.finish_job(claim["job_id"], repaired_run,
                      {"status": "pass", "run_id": repaired_run, "row_count": 60})

    detected = service._record_external_publications(task=ledger.get_production_task("p1"),
                                                     definition=derived_definition())
    assert [item["run_id"] for item in detected] == [repaired_run]
    owed = service.recompute_pending("p1")
    assert [item["reason"] for item in owed] == ["raw_published_outside_the_plan"]
    assert owed[0]["window_start"] == raw["window_start"]

    # Reconciliation re-derives it and clears the debt.
    service.reconcile_publications(limit=5, step_budget=4)
    assert service.recompute_pending("p1") == []


def test_the_first_observation_of_the_raw_layer_is_not_a_repair(tmp_path):
    ledger, service, _ = build(tmp_path)
    service.change("p1", "update", definition=derived_definition(), expected_version=1, now=NOW)
    execution = service.change("p1", "run_now", now=NOW)
    scheduler = Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service)
    scheduler.tick(now=NOW)
    raw = ledger.list_production_steps(execution["execution_id"])[0]
    complete_raw_run(ledger, tmp_path / "lake", start=parse_instant(raw["window_start"]),
                     provider="binance", symbol="BTCUSDT")
    # A plan adopted over an existing raw layer must not recompute all of history.
    assert service._record_external_publications(task=ledger.get_production_task("p1"),
                                                 definition=derived_definition()) == []
    assert service.recompute_pending("p1") == []
    # A second pass over the same parts also owes nothing.
    assert service._record_external_publications(task=ledger.get_production_task("p1"),
                                                 definition=derived_definition()) == []
