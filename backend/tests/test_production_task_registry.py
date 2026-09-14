"""Production plan registry: ownership, tombstones, idempotency and preview.

These are behavioural regression tests for the audited defects: an idempotency
key that could replay a different request, ownership that could not be released,
a delete that was not audited with its tombstone, and a preview that echoed its
input instead of consulting the registry.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from data_center.production_tasks import (
    DefinitionError,
    ProductionConflict,
    ProductionTasks,
    config_facts,
    dependencies,
    next_runs,
    normalize_definition,
    ownership_keys,
)
from data_center.runs.ledger import IdempotencyConflict, RunLedger
from data_center.scheduler import MIN_INTERVAL_SECONDS

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def definition(**overrides) -> dict:
    base = {
        "provider": "dukascopy", "symbol": "EURUSD", "raw_timeframe": "1m", "price_basis": "bid",
        "bar_timeframes": ["5m"],
        "window_policy": {"mode": "continuous", "history_start": "2026-01-01T00:00:00+00:00"},
        "schedule": {"schedule": "fixed_rate", "interval_seconds": 900,
                     "anchor": "2026-09-14T12:00:00+00:00"},
    }
    base.update(overrides)
    return base


@pytest.fixture()
def ledger(tmp_path) -> RunLedger:
    return RunLedger(tmp_path / "ledger.sqlite")


@pytest.fixture()
def service(ledger) -> ProductionTasks:
    return ProductionTasks(ledger)


# -- definitions ---------------------------------------------------------

def test_definition_validation_reports_every_field_error():
    with pytest.raises(DefinitionError) as caught:
        normalize_definition({
            "provider": "dukascopy", "symbol": "NOTAPPROVED", "raw_timeframe": "1d",
            "price_basis": "mid", "bar_timeframes": ["7m"],
            "window_policy": {"mode": "continuous", "history_start": "2026-01-01T00:00:00"},
            "schedule": {"schedule": "fixed_rate", "interval_seconds": 60,
                         "anchor": "2026-09-14T12:00:00+00:00"},
        }, now=NOW)
    fields = {item["field"] for item in caught.value.errors}
    assert {"symbol", "raw_timeframe", "price_basis", "bar_timeframes",
            "window_policy.history_start", "schedule"} <= fields


def test_definition_validation_rejects_a_recipe_the_registry_does_not_have():
    with pytest.raises(DefinitionError) as caught:
        normalize_definition(definition(bar_timeframes=["2h"]), now=NOW)
    assert caught.value.errors[0]["field"] == "bar_timeframes"


def test_ownership_keys_cover_raw_and_every_output():
    normalized = normalize_definition(definition(bar_timeframes=["5m", "1h"]), now=NOW)
    assert ownership_keys(normalized) == [
        "provider_bars:dukascopy:EURUSD:1m:bid",
        "market_bars:dukascopy:EURUSD:5m:bid",
        "market_bars:dukascopy:EURUSD:1h:bid",
    ]


def test_weekly_output_pulls_in_the_daily_recipe_it_depends_on():
    normalized = normalize_definition(definition(bar_timeframes=["1w"]), now=NOW)
    chain = [item["target_timeframe"] for item in dependencies(normalized)]
    # 1w is produced from 1d, so the intermediate recipe is part of the plan.
    assert chain == ["1d", "1w"]


def test_preview_lists_next_runs_conflicts_and_dependencies(ledger, service):
    first = service.create(definition=definition(), name="EURUSD", task_id="p1", now=NOW,
                           desired_state="paused")
    audits_before = len(ledger.write_audit_entries())
    preview = service.preview(definition(), now=NOW)
    assert preview["submittable"] is False
    assert preview["conflicts"] == [{"ownership_key": "provider_bars:dukascopy:EURUSD:1m:bid",
                                     "task_id": "p1"},
                                    {"ownership_key": "market_bars:dukascopy:EURUSD:5m:bid",
                                     "task_id": "p1"}]
    assert preview["schedule"]["next_runs"][:2] == ["2026-09-14T12:00:00+00:00",
                                                    "2026-09-14T12:15:00+00:00"]
    assert len(preview["schedule"]["next_runs"]) == 5
    assert preview["dependencies"][0]["recipe_id"] == "utc-24x7-1m-to-5m-ohlcv"
    assert preview["policy"]["closed_bar_lag_minutes"] == 180
    assert first["definition_version"] == 1
    # The preview is read-only: it must not have queued or audited anything.
    assert len(ledger.write_audit_entries()) == audits_before


def test_fixed_delay_preview_shows_a_rule_instead_of_a_fake_time():
    result = ProductionTasks(_EmptyLedger()).preview(
        definition(schedule={"schedule": "fixed_delay", "interval_seconds": 900}), now=NOW)
    assert result["schedule"]["next_runs"] == []
    assert result["schedule"]["rule"] == "完成后 15 分钟"


class _EmptyLedger:
    path = "empty-ledger"

    @staticmethod
    def ownership_holders(keys):
        return []


def test_config_digest_changes_with_the_resolved_registry_facts():
    normalized = normalize_definition(definition(), now=NOW)
    facts = config_facts(normalized)
    assert facts["instrument"]["symbol"] == "EURUSD"
    assert facts["maintenance_policy"]["policy_id"] == "dukascopy_1m"
    assert facts["recipes"][0]["recipe_version"] == "1"


# -- ownership -----------------------------------------------------------

def test_second_plan_with_the_same_ownership_is_rejected_with_a_stable_code(ledger, service):
    service.create(definition=definition(), name="first", task_id="p1", now=NOW)
    with pytest.raises(ProductionConflict) as caught:
        service.create(definition=definition(), name="second", task_id="p2", now=NOW)
    assert caught.value.code == "ownership_conflict"
    assert "provider_bars:dukascopy:EURUSD:1m:bid" in str(caught.value)
    # Nothing of the rejected plan survives.
    assert ledger.get_production_task("p2") is None


def test_archive_releases_ownership_for_a_new_plan(ledger, service):
    service.create(definition=definition(), name="first", task_id="p1", now=NOW,
                   desired_state="paused")
    service.change("p1", "archive", now=NOW)
    assert ledger.ownership_holders(["provider_bars:dukascopy:EURUSD:1m:bid"]) == []
    # The released key can be taken over, and the earlier holder stays in history.
    service.create(definition=definition(), name="second", task_id="p2", now=NOW)
    history = ledger.ownership_of("p1")
    assert history[0]["state"] == "archived" and history[0]["task_id"] == "p1"


def test_resume_detects_a_key_another_plan_took_over(ledger, service):
    service.create(definition=definition(), name="first", task_id="p1", now=NOW)
    service.change("p1", "archive", now=NOW)
    service.create(definition=definition(), name="second", task_id="p2", now=NOW)
    with pytest.raises(ProductionConflict) as caught:
        service.change("p1", "resume", now=NOW)
    assert caught.value.code == "ownership_conflict"
    assert ledger.get_production_task("p1")["desired_state"] == "archived"


def test_archive_waits_for_an_active_execution(ledger, service):
    service.create(definition=definition(), name="first", task_id="p1", now=NOW)
    ledger.create_production_execution(execution_id="e1", task_id="p1", definition_version=1,
                                       trigger_source="manual")
    with pytest.raises(ProductionConflict) as caught:
        service.change("p1", "archive", now=NOW)
    assert caught.value.code == "active_execution"
    ledger.finish_production_execution("e1", state="completed", outcome="pass")
    assert service.change("p1", "archive", now=NOW)["desired_state"] == "archived"


def test_editing_outputs_swaps_ownership_atomically(ledger, service):
    service.create(definition=definition(), name="first", task_id="p1", now=NOW)
    service.change("p1", "update", definition={"bar_timeframes": ["1h"]},
                   expected_version=1, now=NOW)
    keys = {item["ownership_key"] for item in ledger.ownership_of("p1") if item["state"] == "paused"}
    assert keys == {"provider_bars:dukascopy:EURUSD:1m:bid", "market_bars:dukascopy:EURUSD:1h:bid"}
    # The dropped 5m output is released rather than left claimed.
    assert ledger.ownership_holders(["market_bars:dukascopy:EURUSD:5m:bid"]) == []


# -- tombstones ----------------------------------------------------------

def test_delete_records_a_tombstone_and_audits_it(ledger, service):
    service.create(definition=definition(), name="first", task_id="p1", now=NOW)
    service.change("p1", "pause", now=NOW)
    tombstone = service.change("p1", "delete", now=NOW)
    assert tombstone["outcome"] == "deleted"
    stored = ledger.get_production_task("p1")
    assert stored["deleted_at"] and stored["deleted_digest"] == tombstone["payload_digest"]
    # The key is released for a future plan and the audit row is committed.
    assert ledger.ownership_holders(["provider_bars:dukascopy:EURUSD:1m:bid"]) == []
    entries = ledger.write_audit_entries()
    assert entries[0]["action"] == "production.task.delete"
    assert entries[0]["outcome"] == "deleted"


def test_rejected_delete_is_audited_and_keeps_the_plan(ledger, service):
    service.create(definition=definition(), name="first", task_id="p1", now=NOW,
                   desired_state="enabled")
    with pytest.raises(ProductionConflict):
        service.change("p1", "delete", now=NOW)
    assert ledger.get_production_task("p1")["deleted_at"] is None
    assert ledger.ownership_holders(["provider_bars:dukascopy:EURUSD:1m:bid"])[0]["task_id"] == "p1"
    entries = ledger.write_audit_entries()
    assert entries[0]["outcome"] == "rejected" and entries[0]["code"] == "invalid_state"


def test_alias_resolves_a_deleted_plan_instead_of_returning_nothing(ledger, service):
    service.create(definition=definition(), name="first", task_id="p1", alias="eurusd-live", now=NOW)
    service.change("p1", "archive", now=NOW)
    service.change("p1", "delete", now=NOW)
    resolved = service.read("eurusd-live")
    assert resolved is not None
    assert resolved["task_id"] == "p1" and resolved["health"] == "deleted"


def test_delete_never_touches_runs_or_receipts(ledger, service):
    service.create(definition=definition(), name="first", task_id="p1", now=NOW)
    run_id = ledger.enqueue_job({"job_id": "job-1", "dataset_id": "provider_bars"})
    service.change("p1", "pause", now=NOW)
    service.change("p1", "delete", now=NOW)
    assert ledger.get(run_id)["run_id"] == run_id


# -- idempotency ---------------------------------------------------------

def test_idempotent_action_runs_once_and_replays_the_same_response(ledger, service):
    service.create(definition=definition(), name="first", task_id="p1", now=NOW)
    calls = []

    def action(conn):
        calls.append(1)
        return ledger.set_production_task_state("p1", "paused", conn=conn)

    first = ledger.production_idempotent("key-1", task_id="p1", command="pause", action=action,
                                         request={"command": "pause"})
    replay = ledger.production_idempotent("key-1", task_id="p1", command="pause", action=action,
                                          request={"command": "pause"})
    assert len(calls) == 1
    assert first == replay


def test_idempotency_key_reused_for_different_content_is_a_conflict(ledger, service):
    service.create(definition=definition(), name="first", task_id="p1", now=NOW)
    ledger.production_idempotent("key-1", task_id="p1", command="pause",
                                 action=lambda conn: ledger.set_production_task_state("p1", "paused", conn=conn),
                                 request={"command": "pause"})
    with pytest.raises(IdempotencyConflict):
        ledger.production_idempotent("key-1", task_id="p1", command="resume",
                                     action=lambda conn: ledger.set_production_task_state("p1", "enabled", conn=conn),
                                     request={"command": "resume"})
    # The conflicting replay did not change state.
    assert ledger.get_production_task("p1")["desired_state"] == "paused"


def test_a_failing_action_leaves_no_key_and_no_partial_state(ledger, service):
    service.create(definition=definition(), name="first", task_id="p1", now=NOW)
    attempts = []

    def failing(conn):
        attempts.append(1)
        conn.execute("update production_tasks set name='half-written' where task_id='p1'")
        raise RuntimeError("crashed mid-transaction")

    with pytest.raises(RuntimeError):
        ledger.production_idempotent("key-1", task_id="p1", command="pause", action=failing,
                                     request={"command": "pause"})
    # The write rolled back with the abandoned key, so a retry really retries.
    assert ledger.get_production_task("p1")["name"] == "first"
    retried = ledger.production_idempotent(
        "key-1", task_id="p1", command="pause",
        action=lambda conn: ledger.set_production_task_state("p1", "paused", conn=conn),
        request={"command": "pause"})
    assert len(attempts) == 1 and retried["desired_state"] == "paused"


def test_create_is_idempotent_under_a_repeated_key(ledger, service):
    first = service.create(definition=definition(), name="first", task_id="p1", now=NOW,
                           idempotency_key="create-1")
    replay = service.create(definition=definition(), name="first", task_id="p1", now=NOW,
                            idempotency_key="create-1")
    assert replay == first
    assert len([item for item in ledger.list_production_tasks() if item["task_id"] == "p1"]) == 1
    # A different plan under the same key is a conflict, not a silent replay.
    with pytest.raises(IdempotencyConflict):
        service.create(definition=definition(), name="other", task_id="p2", now=NOW,
                       idempotency_key="create-1")


# -- commands ------------------------------------------------------------

def test_run_now_on_a_paused_plan_conflicts_and_locates_an_active_execution(ledger, service):
    service.create(definition=definition(), name="first", task_id="p1", now=NOW,
                   desired_state="paused")
    with pytest.raises(ProductionConflict) as caught:
        service.change("p1", "run_now", now=NOW)
    assert caught.value.code == "task_paused"
    service.change("p1", "resume", now=NOW)
    first = service.change("p1", "run_now", now=NOW)
    second = service.change("p1", "run_now", now=NOW, idempotency_key="run-2")
    # A plan already running is located, not triggered a second time.
    assert second["execution_id"] == first["execution_id"]
    assert len(ledger.list_production_executions("p1")) == 1


def test_unsupported_command_is_refused_explicitly(ledger, service):
    service.create(definition=definition(), name="first", task_id="p1", now=NOW)
    with pytest.raises(ProductionConflict) as caught:
        service.change("p1", "teleport", now=NOW)
    assert caught.value.code == "unsupported_command"


def test_editing_requires_the_expected_version(ledger, service):
    service.create(definition=definition(), name="first", task_id="p1", now=NOW)
    with pytest.raises(ProductionConflict) as caught:
        service.change("p1", "update", definition={"bar_timeframes": ["1h"]}, now=NOW)
    assert caught.value.code == "expected_version_required"
    with pytest.raises(ProductionConflict) as stale:
        service.change("p1", "update", definition={"bar_timeframes": ["1h"]},
                       expected_version=99, now=NOW)
    assert stale.value.code == "version_conflict"


def test_list_paginates_and_binds_the_cursor_to_the_filters(service):
    for index, symbol in enumerate(("EURUSD", "GBPUSD", "USDCAD")):
        service.create(definition=definition(symbol=symbol, bar_timeframes=[]),
                       name=f"plan-{index}", task_id=f"p{index}", now=NOW)
    first = service.list(page_size=2)
    assert len(first["tasks"]) == 2 and first["page"]["next_cursor"]
    second = service.list(page_size=2, cursor=first["page"]["next_cursor"])
    assert {item["task_id"] for item in first["tasks"]} & {item["task_id"] for item in second["tasks"]} == set()
    with pytest.raises(ProductionConflict) as caught:
        service.list(page_size=2, cursor=first["page"]["next_cursor"], provider="binance")
    assert caught.value.code == "cursor_error"


def test_next_runs_aligns_to_the_anchor_after_a_late_tick():
    normalized = normalize_definition(definition(), now=NOW)
    late = datetime(2026, 9, 14, 12, 47, tzinfo=timezone.utc)
    assert next_runs(normalized, now=late, count=2) == ["2026-09-14T13:00:00+00:00",
                                                        "2026-09-14T13:15:00+00:00"]


def test_config_drift_stops_dispatch_until_it_is_acknowledged(ledger, service):
    service.create(definition=definition(), name="first", task_id="p1", now=NOW,
                   desired_state="enabled")
    # The registry facts moved after the plan was resolved: the persisted digest
    # no longer matches what the plan was approved against.
    ledger.set_config_digest("p1", 1, "stale-digest")
    report = service.reconcile_config_digest()
    assert report["config_drift"] == ["p1"]
    assert ledger.get_production_task("p1")["health"] == "config_drift"
    # A drifting plan is invisible to the due scan and to the claim.
    assert ledger.list_due_production_tasks(now=NOW.isoformat()) == []
    assert ledger.claim_due_execution(task_id="p1", owner_id="one",
                                      scheduled_for=NOW.isoformat(), definition_version=1,
                                      fencing_token=1) is None
    # An explicit confirmation re-baselines the plan and dispatch resumes.
    acknowledged = service.change("p1", "acknowledge_drift", now=NOW)
    assert acknowledged["health"] == "healthy" and acknowledged["config_digest"]
    assert service.reconcile_config_digest()["config_drift"] == []
    assert ledger.get_production_task("p1")["health"] is None


def test_preview_reports_the_plan_contract(service):
    found = service.preview(definition(), now=NOW)
    assert found["minimum_interval_seconds"] == MIN_INTERVAL_SECONDS
    assert set(found["schedule"]) >= {"kind", "next_runs", "rule"}


def test_plan_detail_reports_recorded_progress_not_a_guess(ledger, service):
    service.create(definition=definition(), name="first", task_id="p1", now=NOW,
                   desired_state="enabled")
    empty = service.read("p1")["progress"]
    # Nothing has been planned yet, and the read model says so instead of
    # inventing a frontier.
    assert empty["recorded"] is False and empty["raw_frontier"] is None

    ledger.record_progress("p1", {"frontier": "2026-09-14T11:59:00+00:00",
                                  "effective_end": "2026-09-14T11:59:00+00:00",
                                  "derived_cursor": "2026-09-14T11:55:00+00:00",
                                  "backlog": True, "last_outcome": "pass"})
    service.record_recompute("p1", window_start="2026-09-14T11:00:00+00:00",
                             window_end="2026-09-14T11:05:00+00:00", reason="raw_republished_after_derivation")
    progress = service.read("p1")["progress"]
    assert progress["raw_frontier"] == "2026-09-14T11:59:00+00:00"
    assert progress["derived_cursor"] == "2026-09-14T11:55:00+00:00"
    assert progress["backlog"] is True and progress["last_outcome"] == "pass"
    assert progress["recompute_pending"] == 1
    assert "not live provider freshness" in progress["note"]


def test_plan_phase_and_health_are_read_from_recorded_progress(ledger, service):
    """Spec 4.1: phase and health describe the plan; desired_state stays the intent."""
    service.create(definition=definition(), name="first", task_id="p1", now=NOW,
                   desired_state="enabled")
    # Nothing recorded yet: the plan is initializing, not "healthy by default".
    fresh = service.read("p1")
    assert (fresh["phase"], fresh["health"], fresh["block_reason"]) == ("initializing", "healthy", None)
    assert fresh["desired_state"] == "enabled"

    # A recorded backlog is lagging, and still enabled: health never flips intent.
    ledger.record_progress("p1", {"frontier": "2026-09-14T11:00:00+00:00",
                                  "effective_end": "2026-09-14T11:59:00+00:00",
                                  "backlog": True, "last_outcome": "pass"})
    lagging = service.read("p1")
    assert (lagging["phase"], lagging["health"], lagging["block_reason"]) == (
        "catching_up", "lagging", "backlog")
    assert lagging["desired_state"] == "enabled"

    # An unresolved gap is lagging on its input, not merely behind.
    ledger.record_progress("p1", {"gaps": [{"window_start": "2026-09-14T11:32:00+00:00",
                                            "window_end": "2026-09-14T11:33:00+00:00",
                                            "state": "cooldown", "attempts": 1}]})
    assert service.read("p1")["block_reason"] == "input_unavailable"
    assert service.read("p1")["health"] == "lagging"

    # Outputs waiting on an unavailable dependency are blocked.
    ledger.record_progress("p1", {"deferred_derived": [
        "derive:utc-24x7-1m-to-5m-ohlcv:2026-09-14T11:30:00+00:00:2026-09-14T11:35:00+00:00"]})
    blocked = service.read("p1")
    assert (blocked["phase"], blocked["health"], blocked["block_reason"]) == (
        "catching_up", "blocked", "dependency")

    # Debt cleared and nothing owed: the plan is maintaining.
    ledger.record_progress("p1", {"gaps": [], "deferred_derived": [], "backlog": False,
                                  "last_outcome": "pass"})
    maintaining = service.read("p1")
    assert (maintaining["phase"], maintaining["health"], maintaining["block_reason"]) == (
        "maintaining", "healthy", None)

    # A failed last round is attention, not blocked, and still not paused.
    ledger.record_progress("p1", {"last_outcome": "failed"})
    attention = service.read("p1")
    assert (attention["health"], attention["phase"], attention["desired_state"]) == (
        "attention", "maintaining", "enabled")


def test_plan_list_filters_by_health_and_phase(ledger, service):
    for task_id, symbol, backlog in (("p1", "EURUSD", True), ("p2", "GBPUSD", False)):
        service.create(definition=definition(symbol=symbol), name=task_id, task_id=task_id, now=NOW,
                       desired_state="enabled")
        ledger.record_progress(task_id, {"frontier": "2026-09-14T11:00:00+00:00",
                                         "effective_end": "2026-09-14T11:59:00+00:00",
                                         "backlog": backlog, "last_outcome": "pass"})
    lagging = service.list(health="lagging")
    assert [item["task_id"] for item in lagging["tasks"]] == ["p1"]
    maintaining = service.list(phase="maintaining")
    assert [item["task_id"] for item in maintaining["tasks"]] == ["p2"]
    # The keyset advances over the scanned page, so a filtered page may be empty
    # while the next one still matches; nothing is skipped by the filter.
    first = service.list(health="lagging", page_size=1)
    assert first["tasks"] == [] and first["page"]["next_cursor"]
    second = service.list(health="lagging", page_size=1, cursor=first["page"]["next_cursor"])
    assert [item["task_id"] for item in second["tasks"]] == ["p1"]
    # The cursor stays bound to the filter set it was issued for.
    with pytest.raises(ProductionConflict) as caught:
        service.list(health="healthy", page_size=1, cursor=first["page"]["next_cursor"])
    assert caught.value.code == "cursor_error"
    with pytest.raises(ProductionConflict) as invalid:
        service.list(health="glowing")
    assert invalid.value.code == "filter_error"
