"""Coverage-driven raw planning: tail priority, governed gaps and durable debt.

The legacy maintenance runner already owned these rules; the production
scheduler reuses them instead of re-interpreting them (plan S3.4/S4.4, spec 5.5,
AC12).  These tests pin the scheduler-visible consequences: what gets planned,
in which order, and what stays owed when the provider never supplies a bar.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from data_center import maintenance_runner, window_planner
from data_center.control_plane import evaluate_coverage, timeframe_delta
from data_center.production_tasks import (
    DEFAULT_RAW_TIMEFRAME,
    plan_execution,
    scheduled_end,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
EXECUTION = {"execution_id": "e" * 8}
TASK = {"task_id": "p1"}


def definition(**overrides) -> dict:
    base = {
        "provider": "fixture", "symbol": "UI_TEST", "raw_timeframe": "1m", "price_basis": "raw",
        "bar_timeframes": [],
        "window_policy": {"mode": "continuous", "history_start": "2026-08-01T00:00:00+00:00"},
        "schedule": {"schedule": "manual"},
    }
    base.update(overrides)
    return base


def coverage_at(*, bars: list[datetime], start: datetime, end: datetime):
    return evaluate_coverage(
        dataset_id="provider_bars",
        selector={"provider": "fixture", "symbol": "UI_TEST", "timeframe": DEFAULT_RAW_TIMEFRAME},
        rows=[{"bar_ts": stamp} for stamp in bars],
        timeframe=timeframe_delta(DEFAULT_RAW_TIMEFRAME),
        requested_start=start, requested_end=end)


def covered_bars(*, start: datetime, end: datetime,
                 hole: tuple[datetime, datetime] | None = None) -> list[datetime]:
    stamps, cursor = [], start
    while cursor < end:
        if hole is None or not (hole[0] <= cursor < hole[1]):
            stamps.append(cursor)
        cursor += timedelta(minutes=1)
    return stamps


def plan(**overrides) -> dict:
    arguments = {"now": NOW, "step_budget": 8}
    arguments.update(overrides)
    return plan_execution(TASK, definition(**arguments.pop("definition", {})), EXECUTION, **arguments)


def test_the_scheduler_reuses_the_maintenance_runner_window_rules():
    """One implementation, two callers: the rules cannot drift apart."""
    assert maintenance_runner._exclude_planned_windows is window_planner.exclude_planned_windows
    assert maintenance_runner._tail_recovery_windows is window_planner.tail_recovery_windows
    assert maintenance_runner._recent_gap_windows is window_planner.recent_gap_windows
    # ...and the extracted rule still means what it meant inside the runner.
    assert window_planner.exclude_planned_windows(
        candidates=[{"start": "2026-09-14T00:00:00+00:00", "end": "2026-09-14T06:00:00+00:00"}],
        planned=[{"start": "2026-09-14T00:00:00+00:00", "end": "2026-09-14T06:00:00+00:00"}]) == []


def test_the_observed_tail_is_planned_before_gaps_and_backlog():
    """A permanent gap must not hold back freshness, and the backlog stays bounded."""
    end = scheduled_end(NOW, lag_minutes=1)
    scan_start = end - timedelta(days=3)
    hole = (datetime(2026, 9, 12, 3, 0, tzinfo=timezone.utc), datetime(2026, 9, 12, 3, 5, tzinfo=timezone.utc))
    # Bars stop before the effective end, so the recent suffix is simply late.
    bars = covered_bars(start=scan_start, end=end - timedelta(minutes=9), hole=hole)
    result = plan(coverage=coverage_at(bars=bars, start=scan_start, end=end), step_budget=3)

    assert [step["reason"] for step in result["steps"]] == ["tail", "gap_repair", "backfill"]
    # The late suffix comes first, then the interior gap, then the backlog.
    assert result["steps"][0]["window_end"] == end.isoformat()
    assert result["steps"][1]["window_start"] == hole[0].isoformat()
    assert result["steps"][1]["window_end"] == hole[1].isoformat()
    assert result["truncated_windows"] > 0
    # What the provider has shown is reported next to what the plan planned.
    assert result["observed_boundary"] == (end - timedelta(minutes=10)).isoformat()
    assert result["complete_boundary"] == hole[0].isoformat()


def test_the_frontier_only_advances_over_work_the_round_planned():
    end = scheduled_end(NOW, lag_minutes=1)
    scan_start = end - timedelta(days=3)
    # The recent suffix is late, so the tail is planned first and the backlog is
    # what the budget leaves over.
    bars = covered_bars(start=scan_start, end=end - timedelta(minutes=9))
    result = plan(coverage=coverage_at(bars=bars, start=scan_start, end=end), step_budget=2)

    assert [step["reason"] for step in result["steps"]] == ["tail", "backfill"]
    # The frontier may not jump over the history between the recorded frontier
    # and the tail: only the shard this round actually planned counts.
    assert result["frontier"] == result["steps"][1]["window_end"]
    assert result["frontier"] < result["planned_end"]


def test_a_scan_range_the_provider_already_covered_plans_nothing():
    end = scheduled_end(NOW, lag_minutes=1)
    scan_start = end - timedelta(days=3)
    bars = covered_bars(start=scan_start, end=end)
    result = plan(coverage=coverage_at(bars=bars, start=scan_start, end=end),
                  progress={"frontier": end.isoformat()}, step_budget=8)

    assert result["steps"] == [] and result["reason"] == "no_work"
    # Coverage, not the cursor, is what says the data is complete.
    assert result["complete_boundary"] == end.isoformat()
    assert result["backlog"] is False


def test_a_gap_inside_its_retry_cooldown_is_reported_not_replanned():
    """AC12: gap cooldown does not manufacture a failing fetch every tick."""
    end = scheduled_end(NOW, lag_minutes=1)
    scan_start = end - timedelta(days=3)
    hole = (datetime(2026, 9, 12, 3, 0, tzinfo=timezone.utc), datetime(2026, 9, 12, 3, 5, tzinfo=timezone.utc))
    bars = covered_bars(start=scan_start, end=end, hole=hole)
    coverage = coverage_at(bars=bars, start=scan_start, end=end)

    planned = plan(coverage=coverage, progress={"frontier": end.isoformat()},
                   cooldown_windows=[{"start": hole[0].isoformat(), "end": hole[1].isoformat()}])
    assert [step["reason"] for step in planned["steps"]] == []
    assert planned["gaps"] == [{"window_start": hole[0].isoformat(), "window_end": hole[1].isoformat(),
                                "state": "cooldown", "source": "coverage"}]

    # Once the cooldown expires, exactly that window is fetched again.
    after_cooldown = plan(coverage=coverage, progress={"frontier": end.isoformat()},
                          cooldown_windows=[])
    assert [(step["reason"], step["window_start"], step["window_end"])
            for step in after_cooldown["steps"]] == [("gap_repair", hole[0].isoformat(),
                                                      hole[1].isoformat())]
    assert after_cooldown["gaps"] == [{"window_start": hole[0].isoformat(),
                                       "window_end": hole[1].isoformat(),
                                       "state": "planned", "source": "gap_repair"}]

    # A cooldown that covers only part of the hole releases the rest of it.
    partial = plan(coverage=coverage, progress={"frontier": end.isoformat()},
                   cooldown_windows=[{"start": hole[0].isoformat(),
                                      "end": (hole[0] + timedelta(minutes=2)).isoformat()}])
    assert [(step["reason"], step["window_start"]) for step in partial["steps"]] == [
        ("gap_repair", (hole[0] + timedelta(minutes=2)).isoformat())]


def plan_ledger(directory: str):
    """An isolated ledger with one enabled plan, which is what makes jobs claimable."""
    from pathlib import Path

    from data_center.runs.ledger import RunLedger

    ledger = RunLedger(Path(directory) / "ledger.sqlite")
    ledger.create_production_task(task_id="p1", name="UI_TEST", payload=definition(),
                                 ownership_keys=["fixture:UI_TEST:provider_bars:1m"],
                                 desired_state="enabled", provider="fixture", symbol="UI_TEST")
    return ledger


def test_a_window_whose_run_failed_is_planned_again_from_the_ledger():
    """Debt is derived from step state, so the watermark cannot retire a gap."""
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        ledger = plan_ledger(directory)
        window = ("2026-09-12T03:00:00+00:00", "2026-09-12T03:05:00+00:00")
        ledger.accept_execution_plan(
            execution_id="e" * 8, task_id="p1", definition_version=1, trigger_source="manual",
            steps=[{"stage": "raw", "window_start": window[0], "window_end": window[1],
                    "dedupe_key": f"raw:{window[0]}:{window[1]}",
                    "payloads": [{"job_id": "p1:deadbeef:raw:gap_repair",
                                  "dataset_id": "provider_bars", "provider": "fixture",
                                  "symbol": "UI_TEST", "asset_class": "crypto", "timeframe": "1m",
                                  "start": window[0], "end": window[1], "run_scope": "production",
                                  "run_kind": "gap_repair"}]}])
        claim = ledger.claim_next_job()
        ledger.fail_job(claim["job_id"], claim["run_id"], "provider exploded",
                        error_type="ProviderError", retryable=False)

        # Step state is read back from the run outcomes, exactly as dispatch does.
        ledger.refresh_task_steps("p1")
        owed = ledger.outstanding_gap_windows("p1")
        assert [(item["window_start"], item["window_end"]) for item in owed] == [window]
        # ...and the planner fetches it again even though the catalog scan is short.
        result = plan(progress={"frontier": "2026-09-14T12:02:00+00:00"}, carried_gaps=owed,
                      step_budget=2)
        assert [(step["reason"], step["window_start"], step["window_end"])
                for step in result["steps"]] == [("gap_repair", window[0], window[1])]

        # A later completed run for the same window retires the debt.  The plan
        # may only hold one non-terminal round, so the failed one closes first.
        ledger.finish_production_execution("e" * 8, state="failed", outcome="failed")
        ledger.accept_execution_plan(
            execution_id="f" * 8, task_id="p1", definition_version=1, trigger_source="manual",
            steps=[{"stage": "raw", "window_start": window[0], "window_end": window[1],
                    "dedupe_key": f"raw:{window[0]}:{window[1]}",
                    "payloads": [{"job_id": "p1:deadbeef:raw:gap_repair",
                                  "dataset_id": "provider_bars", "provider": "fixture",
                                  "symbol": "UI_TEST", "asset_class": "crypto", "timeframe": "1m",
                                  "start": window[0], "end": window[1], "run_scope": "production",
                                  "run_kind": "gap_repair"}]}])
        retry = ledger.claim_next_job()
        ledger.finish_job(retry["job_id"], retry["run_id"], {"status": "pass", "run_id": retry["run_id"]})
        ledger.refresh_task_steps("p1")
        assert ledger.outstanding_gap_windows("p1") == []


@pytest.mark.parametrize("state", ["failed", "dead_letter"])
def test_gap_debt_keeps_terminal_states_that_are_not_completed(state):
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        ledger = plan_ledger(directory)
        window = ("2026-09-12T03:00:00+00:00", "2026-09-12T03:05:00+00:00")
        ledger.accept_execution_plan(
            execution_id="e" * 8, task_id="p1", definition_version=1, trigger_source="manual",
            steps=[{"stage": "raw", "window_start": window[0], "window_end": window[1],
                    "dedupe_key": f"raw:{window[0]}:{window[1]}",
                    "payloads": [{"job_id": "p1:deadbeef:raw:ingest", "dataset_id": "provider_bars",
                                  "provider": "fixture", "symbol": "UI_TEST", "asset_class": "crypto",
                                  "timeframe": "1m", "start": window[0], "end": window[1],
                                  "run_scope": "production", "run_kind": "ingest"}]}])
        claim = ledger.claim_next_job()
        ledger.fail_job(claim["job_id"], claim["run_id"], "boom", error_type="ProviderError",
                        retryable=False)
        with ledger._connect() as conn:
            conn.execute("update production_steps set state=? where stage='raw'", (state,))
        assert len(ledger.outstanding_gap_windows("p1")) == 1


def publish_bars(root, *, start: datetime, minutes: int, run_id: str,
                 hole: tuple[datetime, datetime] | None = None) -> None:
    """Publish one raw 1m part covering ``minutes`` from ``start`` except a hole."""
    from data_center.catalog.manifest import build_manifest, write_manifest
    from data_center.domain.models import ProviderBar
    from data_center.storage.parquet import write_provider_bars

    rows = [ProviderBar(symbol="UI_TEST", asset_class="crypto", provider="fixture", timeframe="1m",
                        bar_ts=start + timedelta(minutes=index), open=100, high=102, low=99,
                        close=101, volume=1, currency="USD", price_type="raw",
                        ingest_ts=start + timedelta(hours=1), source_hash=f"raw-{index}")
            for index in range(minutes)
            if hole is None or not (hole[0] <= start + timedelta(minutes=index) < hole[1])]
    paths = write_provider_bars(root, rows, part_id=run_id)
    write_manifest(root, build_manifest(
        root, run_id=run_id, dataset_id="provider_bars", schema_version="provider_bars.v1",
        paths=paths, row_count=len(rows),
        quality_summary={"status": "pass", "finding_count": 0, "findings": []}))


def register_fixture_instrument() -> None:
    """The fixture provider is the documented isolated acceptance path."""
    from data_center.control_plane import InstrumentMetadata
    from data_center.platform_registry import REGISTRY

    if not any(item.symbol == "UI_TEST" for item in REGISTRY.instruments("fixture", approved_only=False)):
        REGISTRY.register_instrument(InstrumentMetadata(
            provider="fixture", symbol="UI_TEST", canonical_symbol="UI_TEST", asset_class="crypto",
            currency="USD", session_profile="utc_24x7", calendar_profile="crypto_24x7_v1"))


def test_dispatch_records_what_the_provider_showed_and_what_stays_owed(tmp_path):
    """The console shows recorded boundaries, so the debt has to be recorded too."""
    from data_center.production_tasks import ProductionTasks
    from data_center.runs.ledger import RunLedger
    from data_center.scheduler import Scheduler

    register_fixture_instrument()

    end = scheduled_end(NOW, lag_minutes=1)
    scan_start = end - timedelta(days=3)
    hole = (datetime(2026, 9, 13, 3, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 13, 3, 5, tzinfo=timezone.utc))
    root = tmp_path / "lake"
    publish_bars(root, start=scan_start, minutes=int((end - scan_start).total_seconds() // 60),
                 run_id="raw-part", hole=hole)

    ledger = RunLedger(tmp_path / "ledger.sqlite")
    service = ProductionTasks(ledger, canonical_root=root)
    task = service.create(definition=definition(window_policy={
        "mode": "continuous", "history_start": scan_start.isoformat()}), name="UI_TEST",
        task_id="p1", desired_state="enabled", now=NOW)
    execution = service.change("p1", "run_now", now=NOW)
    Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service).tick(now=NOW)

    progress = service.read("p1")["progress"]
    # The observed and complete boundaries answer different questions, and the
    # plan reports both instead of presenting its own plan as freshness.
    assert progress["observed_boundary"] == (end - timedelta(minutes=1)).isoformat()
    assert progress["complete_boundary"] == hole[0].isoformat()
    assert [(gap["window_start"], gap["window_end"], gap["state"]) for gap in progress["gaps"]] == [
        (hole[0].isoformat(), hole[1].isoformat(), "planned")]
    assert progress["recorded"] is True

    # The round fetched exactly the missing window, and says why.
    runs = [run for run in ledger.list() if run.get("execution_id") == execution["execution_id"]]
    assert [(datetime.fromisoformat(str(run["start"])), datetime.fromisoformat(str(run["end"])),
             run["execution_plan"]["windows"][0]["reason"]) for run in runs] == [
        (hole[0], hole[1], "gap_repair")]
    assert task["task_id"] == "p1"


def capacity_snapshot(status: str):
    from data_center.capacity import CapacitySnapshot

    ratios = {"ok": 0.5, "warning": 0.12, "critical": 0.05}
    free = ratios[status]
    return CapacitySnapshot(total_bytes=100, used_bytes=int(100 * (1 - free)), free_bytes=int(100 * free),
                            free_ratio=free, status=status, warning_free_ratio=0.15,
                            critical_free_ratio=0.10, measurement_source="pinned")


def test_a_capacity_warning_blocks_the_unattended_backlog_without_hiding_it():
    """AC08: a long backfill is reported as blocked, not sharded past the gate."""
    end = scheduled_end(NOW, lag_minutes=1)
    scan_start = end - timedelta(days=3)
    bars = covered_bars(start=scan_start, end=end - timedelta(minutes=9))
    coverage = coverage_at(bars=bars, start=scan_start, end=end)

    # A far-behind frontier is a backlog of months, planned under a warning.
    blocked = plan(coverage=coverage, progress={"frontier": "2026-06-01T00:00:00+00:00"},
                   capacity_status="warning", step_budget=8)
    assert blocked["backlog_blocked"] is True and blocked["backlog"] is True
    assert blocked["backlog_span_days"] > 31
    # The recent tail is still fetched: freshness never waits for capacity.
    assert [step["reason"] for step in blocked["steps"]] == ["tail"]
    # The frontier stays where the plan starts: no backlog window was planned, and
    # the plan says so instead of pretending it caught up to the last days.
    assert blocked["frontier"] == "2026-08-01T00:00:00+00:00" == blocked["planned_start"]

    # Without the warning the same plan advances the backlog normally.
    allowed = plan(coverage=coverage, progress={"frontier": "2026-06-01T00:00:00+00:00"},
                   capacity_status="ok", step_budget=8)
    assert allowed["backlog_blocked"] is False
    assert "backfill" in [step["reason"] for step in allowed["steps"]]
    assert allowed["frontier"] > "2026-06-01T00:00:00+00:00"

    # A short catch-up is not an unattended backfill and is never gated.
    short = plan(coverage=coverage, progress={"frontier": scan_start.isoformat()},
                 capacity_status="warning", step_budget=8)
    assert short["backlog_blocked"] is False and short["backlog_span_days"] <= 31


def test_a_critical_capacity_state_refuses_new_publishing_dispatch(tmp_path):
    """AC08: critical stops new publishing work and leaves the backlog readable."""
    from data_center.capacity import FixedCapacityPolicy
    from data_center.production_tasks import ProductionTasks
    from data_center.runs.ledger import RunLedger
    from data_center.scheduler import Scheduler

    register_fixture_instrument()
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    service = ProductionTasks(ledger, canonical_root=tmp_path / "lake",
                              capacity_policy=FixedCapacityPolicy(capacity_snapshot("critical")))
    service.create(definition=definition(), name="UI_TEST", task_id="p1", desired_state="enabled",
                   now=NOW)
    execution = service.change("p1", "run_now", now=NOW)

    gate = service.dispatch_gate(now=NOW)
    assert gate == {"allowed": False, "block_reason": "capacity",
                    "capacity": {**gate["capacity"], "status": "critical"}, "now": NOW.isoformat()}

    result = Scheduler(ledger, instance_id="one", dispatch_enabled=True,
                       planner=service).tick(now=NOW)
    decision = next(item for item in result["decisions"] if item.get("reason") == "capacity")
    assert decision["action"] == "capacity_blocked" and decision["capacity_status"] == "critical"
    # No slot was consumed and no step, run or job was created: the round waits.
    assert ledger.list_production_steps(execution["execution_id"]) == []
    assert ledger.list() == []
    assert ledger.get_production_execution(execution["execution_id"])["state"] == "pending"
    assert result["dispatched"] == 0

    # The plan reports the gate and stays readable.
    document = service.read("p1")
    assert (document["health"], document["block_reason"], document["phase"]) == (
        "blocked", "capacity", "catching_up")
    assert document["progress"]["capacity_block"]["status"] == "critical"

    # Dispatch refuses even when a caller claims a slot itself, so the gate
    # cannot be walked around.
    direct = service.dispatch(task=ledger.get_production_task("p1"), execution=execution, now=NOW)
    assert direct["planned_steps"] == 0 and direct["blocked"] == "capacity"

    # Once capacity recovers the same round is planned and the block is cleared.
    service.capacity_policy = FixedCapacityPolicy(capacity_snapshot("ok"))
    recovered = Scheduler(ledger, instance_id="one", dispatch_enabled=True,
                          planner=service).tick(now=NOW + timedelta(minutes=1))
    assert recovered["dispatched"] == 1
    assert ledger.get_production_execution(execution["execution_id"])["state"] == "running"
    assert service.read("p1")["progress"]["capacity_block"] is None


def test_the_scheduler_view_reports_capacity_and_provider_backoff(tmp_path):
    from data_center.runs.ledger import RunLedger

    ledger = RunLedger(tmp_path / "ledger.sqlite", clock=lambda: NOW.timestamp())
    ledger.enqueue_job({"job_id": "j1", "dataset_id": "provider_bars", "provider": "fixture",
                        "symbol": "UI_TEST", "timeframe": "1d", "start": "2026-09-14T00:00:00Z",
                        "end": "2026-09-14T01:00:00Z", "run_scope": "production"})
    claim = ledger.claim_next_job()
    # A retryable failure pushes the job's own retry past now: that is backoff.
    ledger.fail_job(claim["job_id"], claim["run_id"], "transient", retryable=True,
                    delay_seconds=300.0)
    waiting = ledger.provider_backoff_state(now=NOW)
    assert [item["provider"] for item in waiting] == ["fixture"]
    assert waiting[0]["waiting"] == 1 and waiting[0]["next_attempt_at"] > NOW.isoformat()
    # Nothing is waiting once the job's own delay has elapsed.
    assert ledger.provider_backoff_state(now=NOW + timedelta(hours=1)) == []


def test_spent_retry_budgets_back_a_provider_off_without_stopping_others(tmp_path):
    """AC13: transient failures back off; other providers keep producing."""
    from data_center.production_tasks import ProductionTasks
    from data_center.runs.ledger import RunLedger
    from data_center.scheduler import Scheduler

    register_fixture_instrument()
    ledger = RunLedger(tmp_path / "ledger.sqlite", clock=lambda: NOW.timestamp())
    service = ProductionTasks(ledger, canonical_root=tmp_path / "lake")
    service.create(definition=definition(), name="first", task_id="p1", desired_state="enabled",
                   now=NOW)
    # A second plan on another provider must not inherit the first one's backoff.
    service.create(definition=definition(provider="binance", symbol="BTCUSDT", raw_timeframe="1m",
                                         price_basis="raw"),
                   name="second", task_id="p2", desired_state="enabled", now=NOW)

    first = service.change("p1", "run_now", now=NOW)
    Scheduler(ledger, instance_id="one", dispatch_enabled=True, planner=service).tick(now=NOW)
    # The provider fails every attempt the worker was allowed.  The worker keeps
    # retrying while the budget lasts; only the spent budget is evidence the
    # scheduler may act on, so the run has to reach its terminal state.
    for _ in range(3):
        claim = ledger.claim_next_job()
        if claim is None:
            break
        ledger.fail_job(claim["job_id"], claim["run_id"], "provider exploded",
                        error_type="ProviderError", retryable=True)

    result = service.reconcile(now=NOW + timedelta(minutes=1), limit=10)
    recorded = [item for item in result["provider_backoff_recorded"] if item["task_id"] == "p1"]
    assert recorded, result
    assert recorded[0]["failures"] >= 1 and recorded[0]["reason"] == "provider_transient_failures"
    assert [item["provider"] for item in ledger.provider_backoff(now=NOW + timedelta(minutes=1))] == [
        "fixture"]

    # While the provider cools down, this plan makes no new request...
    blocked = service.dispatch(task=ledger.get_production_task("p1"),
                               execution={"execution_id": first["execution_id"]}, now=NOW + timedelta(minutes=2))
    assert blocked["planned_steps"] == 0 and blocked["blocked"] == "provider_backoff"
    document = service.read("p1")
    assert (document["health"], document["block_reason"]) == ("lagging", "provider_backoff")
    assert document["progress"]["provider_backoff"]["failures"] >= 1

    # ...and the other provider is untouched: it plans and runs.
    second = service.change("p2", "run_now", now=NOW + timedelta(minutes=2))
    assert second["execution_id"]
    other = service.dispatch(task=ledger.get_production_task("p2"), execution=second,
                             now=NOW + timedelta(minutes=2))
    assert other["planned_steps"] >= 1 and other.get("blocked") is None

    # Once the backoff elapses the first plan is planned again, and the recorded
    # copy of the refusal is cleared.
    later = NOW + timedelta(hours=2)
    resumed = service.dispatch(task=ledger.get_production_task("p1"), execution=first, now=later)
    assert resumed["planned_steps"] >= 1 and resumed.get("blocked") is None
    assert ledger.provider_backoff(now=later) == []
    # The read model follows the ledger's own clock, so it is advanced with it:
    # an expired backoff is no longer reported as this plan's block.
    ledger.clock = lambda: later.timestamp()
    document = service.read("p1")
    assert document["progress"]["provider_backoff"] is None
    assert document["block_reason"] != "provider_backoff"
    # The windows the provider never produced are still owed, and are still
    # reported as such: a spent backoff must not look like a repair.
    assert document["block_reason"] == "input_unavailable"
