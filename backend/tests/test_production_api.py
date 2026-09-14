"""HTTP contract for production plans: preview, create, change, list and history.

The routes are one adapter over the production task module, so these tests check
the externally visible promises: a preview that queues nothing, field-level
validation errors, stable conflict codes, idempotent writes, version conflicts
and cursor pagination bound to its filters (AC01, AC07, AC16).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from data_center.api.app import create_app
from data_center.production_tasks import ProductionTasks
from data_center.runs.ledger import RunLedger
from data_center.settings import Settings

KEY = "production-api-key"
NOW = "2026-09-14T12:00:00+00:00"
# The API validates against the real clock, so a fixed anchor would silently
# become "in the past" as the suite ages.  The anchor is therefore an hour ahead
# of now, on a quarter-hour boundary.
ANCHOR = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def definition(**overrides) -> dict:
    base = {
        "provider": "dukascopy", "symbol": "EURUSD", "raw_timeframe": "1m", "price_basis": "bid",
        "bar_timeframes": ["5m"],
        "window_policy": {"mode": "continuous", "history_start": "2026-01-01T00:00:00+00:00"},
        "schedule": {"schedule": "fixed_rate", "interval_seconds": 900,
                     "anchor": ANCHOR.isoformat()},
    }
    base.update(overrides)
    return base


@pytest.fixture()
def config(tmp_path) -> Settings:
    return Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                    evidence_root=tmp_path / "evidence", api_key=KEY)


@pytest.fixture()
def client(config) -> TestClient:
    return TestClient(create_app(config))


def auth() -> dict:
    return {"X-API-Key": KEY}


def create_plan(client, task_id="p1", **overrides) -> dict:
    # ``desired_state`` belongs to the request, not to the plan definition.
    desired_state = overrides.pop("desired_state", "paused")
    body = {"task_id": task_id, "name": overrides.pop("name", task_id),
            "desired_state": desired_state, "definition": definition(**overrides)}
    response = client.post("/api/v1/production/tasks", json=body, headers=auth())
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_preview_needs_no_key_and_writes_nothing(client, config):
    response = client.post("/api/v1/production/plans", json={"definition": definition()})
    assert response.status_code == 200
    preview = response.json()["data"]
    assert preview["submittable"] is True
    assert preview["ownership_keys"] == ["provider_bars:dukascopy:EURUSD:1m:bid",
                                         "market_bars:dukascopy:EURUSD:5m:bid"]
    assert preview["schedule"]["next_runs"][:2] == [
        ANCHOR.isoformat(), (ANCHOR + timedelta(minutes=15)).isoformat()]
    assert preview["dependencies"][0]["recipe_id"] == "utc-24x7-1m-to-5m-ohlcv"
    ledger = RunLedger(config.ledger_path)
    assert ledger.list_production_tasks() == []
    assert ledger.write_audit_entries() == []


def test_preview_reports_field_errors_without_a_key(client):
    response = client.post("/api/v1/production/plans",
                           json={"definition": {"provider": "dukascopy", "symbol": "NOPE"}})
    assert response.status_code == 200
    preview = response.json()["data"]
    assert preview["submittable"] is False
    assert "symbol" in {item["field"] for item in preview["validation"]["errors"]}


def test_create_persists_definition_ownership_and_one_audit_entry(client, config):
    created = create_plan(client)
    assert created["task_id"] == "p1" and created["desired_state"] == "paused"
    ledger = RunLedger(config.ledger_path)
    task = ledger.get_production_task("p1")
    assert task["provider"] == "dukascopy" and task["symbol"] == "EURUSD"
    assert task["next_run_at"] == ANCHOR.isoformat()
    assert [item["ownership_key"] for item in ledger.ownership_of("p1")] == [
        "provider_bars:dukascopy:EURUSD:1m:bid", "market_bars:dukascopy:EURUSD:5m:bid"]
    entries = ledger.write_audit_entries()
    assert [item["action"] for item in entries] == ["production.task.create"]
    assert entries[0]["outcome"] == "created"


def test_create_rejects_an_unapproved_symbol_with_field_errors(client):
    response = client.post("/api/v1/production/tasks",
                           json={"name": "bad", "definition": definition(symbol="NOPE")},
                           headers=auth())
    assert response.status_code == 422
    errors = response.json()["errors"]
    assert errors[0]["code"] == "invalid_definition"
    assert {item["field"] for item in errors if "field" in item} == {"symbol"}


def test_second_plan_for_the_same_output_is_a_stable_conflict(client):
    create_plan(client, "p1")
    response = client.post("/api/v1/production/tasks",
                           json={"task_id": "p2", "name": "second", "definition": definition()},
                           headers=auth())
    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "ownership_conflict"


def test_create_is_idempotent_with_a_key(client, config):
    body = {"task_id": "p1", "name": "first", "definition": definition()}
    headers = {**auth(), "Idempotency-Key": "create-1"}
    first = client.post("/api/v1/production/tasks", json=body, headers=headers)
    replay = client.post("/api/v1/production/tasks", json=body, headers=headers)
    assert first.status_code == 201 and replay.status_code == 201
    assert first.json()["data"] == replay.json()["data"]
    ledger = RunLedger(config.ledger_path)
    assert len(ledger.list_production_tasks()) == 1
    # The replay is audited too, so a repeated key is visible without repeating
    # the side effect (entries are newest-first).
    assert sorted(item["outcome"] for item in ledger.write_audit_entries()) == ["created", "replayed"]


def test_actions_require_a_key_and_audit_a_refusal(client, config):
    unauthenticated = client.post("/api/v1/production/tasks/p1/actions", json={"command": "pause"})
    assert unauthenticated.status_code == 401
    missing = client.post("/api/v1/production/tasks/absent/actions", json={"command": "pause"},
                          headers=auth())
    assert missing.status_code == 404
    entries = RunLedger(config.ledger_path).write_audit_entries()
    assert [(item["action"], item["outcome"]) for item in entries] == [
        ("production.task.pause", "rejected")]


def test_pause_resume_and_run_now_follow_the_plan_state(client, config):
    create_plan(client, desired_state="enabled")
    paused = client.post("/api/v1/production/tasks/p1/actions", json={"command": "pause"},
                         headers=auth())
    assert paused.status_code == 200 and paused.json()["data"]["desired_state"] == "paused"
    # A paused plan refuses an immediate run instead of silently executing.
    refused = client.post("/api/v1/production/tasks/p1/actions", json={"command": "run_now"},
                          headers=auth())
    assert refused.status_code == 409 and refused.json()["errors"][0]["code"] == "task_paused"
    assert client.post("/api/v1/production/tasks/p1/actions", json={"command": "resume"},
                       headers=auth()).json()["data"]["desired_state"] == "enabled"
    started = client.post("/api/v1/production/tasks/p1/actions", json={"command": "run_now"},
                          headers=auth())
    assert started.status_code == 200
    execution_id = started.json()["data"]["execution_id"]
    # Triggering again locates the execution already in flight.
    again = client.post("/api/v1/production/tasks/p1/actions", json={"command": "run_now"},
                        headers=auth())
    assert again.json()["data"]["execution_id"] == execution_id
    history = client.get("/api/v1/production/tasks/p1/executions", headers=auth())
    assert [item["execution_id"] for item in history.json()["data"]] == [execution_id]
    ledger = RunLedger(config.ledger_path)
    assert ledger.get_production_execution(execution_id)["trigger_source"] == "manual"


def test_edit_requires_expected_version_and_rejects_a_stale_one(client):
    create_plan(client)
    missing = client.patch("/api/v1/production/tasks/p1",
                           json={"definition": {"bar_timeframes": ["1h"]}}, headers=auth())
    assert missing.status_code == 422
    assert missing.json()["errors"][0]["code"] == "expected_version_required"
    stale = client.patch("/api/v1/production/tasks/p1",
                         json={"definition": {"bar_timeframes": ["1h"]}, "expected_version": 99},
                         headers=auth())
    assert stale.status_code == 409
    assert stale.json()["errors"][0]["code"] == "version_conflict"
    ok = client.patch("/api/v1/production/tasks/p1",
                      json={"definition": {"bar_timeframes": ["1h"]}, "expected_version": 1},
                      headers=auth())
    assert ok.status_code == 200 and ok.json()["data"]["definition_version"] == 2


def test_list_filters_and_paginates_with_a_cursor_bound_to_the_filters(client):
    for index, symbol in enumerate(("EURUSD", "GBPUSD", "USDCAD")):
        create_plan(client, f"p{index}", symbol=symbol, bar_timeframes=[])
    first = client.get("/api/v1/production/tasks", params={"page_size": 2}, headers=auth())
    page = first.json()["meta"]["page"]
    assert len(first.json()["data"]) == 2 and page["next_cursor"]
    seen = {item["task_id"] for item in first.json()["data"]}
    second = client.get("/api/v1/production/tasks",
                        params={"page_size": 2, "cursor": page["next_cursor"]}, headers=auth())
    assert seen.isdisjoint({item["task_id"] for item in second.json()["data"]})
    mismatched = client.get("/api/v1/production/tasks",
                            params={"page_size": 2, "cursor": page["next_cursor"],
                                    "provider": "dukascopy", "symbol": "EURUSD"}, headers=auth())
    assert mismatched.status_code == 422
    assert mismatched.json()["errors"][0]["code"] == "cursor_error"
    filtered = client.get("/api/v1/production/tasks", params={"symbol": "GBPUSD"}, headers=auth())
    assert [item["symbol"] for item in filtered.json()["data"]] == ["GBPUSD"]


def test_deleted_plan_stays_resolvable_through_its_alias(client):
    create_plan(client)
    client.patch("/api/v1/production/tasks/p1", json={"desired_state": "archived"}, headers=auth())
    deleted = client.post("/api/v1/production/tasks/p1/actions", json={"command": "delete"},
                          headers=auth())
    assert deleted.status_code == 200
    assert deleted.json()["data"]["outcome"] == "deleted"
    detail = client.get("/api/v1/production/tasks/p1", headers=auth())
    assert detail.status_code == 200
    assert detail.json()["data"]["health"] == "deleted"
    assert detail.json()["data"]["deleted_digest"]
    listed = client.get("/api/v1/production/tasks", headers=auth())
    assert listed.json()["data"] == []


def test_plan_list_filters_on_read_model_health(client, config):
    """Spec 8: the list filters on provider/symbol/state/health with a bound cursor."""
    create_plan(client, "p1", desired_state="enabled")
    RunLedger(config.ledger_path).record_progress("p1", {
        "frontier": "2026-09-14T11:00:00+00:00", "effective_end": "2026-09-14T11:59:00+00:00",
        "backlog": True, "last_outcome": "pass"})
    listed = client.get("/api/v1/production/tasks?health=lagging", headers=auth()).json()["data"]
    assert [item["task_id"] for item in listed] == ["p1"]
    assert listed[0]["phase"] == "catching_up" and listed[0]["block_reason"] == "backlog"
    healthy = client.get("/api/v1/production/tasks?health=healthy", headers=auth()).json()
    assert healthy["data"] == []
    invalid = client.get("/api/v1/production/tasks?health=glowing", headers=auth())
    assert invalid.status_code == 422
    assert invalid.json()["errors"][0]["code"] == "filter_error"


def test_capabilities_route_reports_the_plan_contract(client):
    """A console enables a plan option only because this read model says it can."""
    response = client.get("/api/v1/capabilities", headers=auth())
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["providers"]
    production = data["production"]
    assert production["schedule_kinds"] == ["daily", "fixed_delay", "fixed_rate", "manual", "once"]
    assert production["minimum_interval_seconds"] == 300
    assert production["plan_states"] == ["enabled", "paused", "archived"]
    assert {"healthy", "lagging", "blocked", "attention", "config_drift"} <= set(production["plan_health"])
    assert production["plan_phases"] == ["catching_up", "initializing", "maintaining"]
    assert {"paused", "global_pause", "config_drift", "dependency", "input_unavailable", "backlog"} <= set(
        production["block_reasons"])
    assert production["scheduler_enabled"] is True
    assert {item["dataset_id"] for item in production["outputs"]} == {"provider_bars", "market_bars"}


def test_operations_scheduler_reports_state_and_due_plans(client, config):
    create_plan(client, "p1", desired_state="enabled")
    payload = client.get("/api/v1/operations/scheduler", headers=auth()).json()["data"]
    # A freshly created plan is scheduled ahead, and a scheduler that never ran
    # reports no heartbeat instead of an invented healthy one.
    assert payload["due_now"] == 0
    assert payload["scheduler"]["heartbeat_at"] is None
    assert payload["dispatch_enabled"] is True
    assert payload["plans_by_state"] == {"enabled": 1}
    # Once a slot is genuinely in the past the view lists it (the endpoint
    # compares against the real clock, so the slot must really have passed).
    RunLedger(config.ledger_path).set_task_next_run_at(
        task_id="p1", next_run_at="2020-01-01T00:00:00+00:00")
    payload = client.get("/api/v1/operations/scheduler", headers=auth()).json()["data"]
    assert payload["due_task_ids"] == ["p1"]
    assert payload["oldest_due_at"] == "2020-01-01T00:00:00+00:00"


def test_scheduler_view_reports_the_capacity_gate_and_provider_backoff(client, config):
    """Spec 5.6/9.1: capacity protection and provider backoff are observed values."""
    payload = client.get("/api/v1/operations/scheduler", headers=auth()).json()["data"]
    assert set(payload) >= {"capacity", "publishing_allowed", "provider_backoff", "blocked"}
    # The gate is the measurement, not a promise: critical is what refuses work.
    assert payload["publishing_allowed"] is (payload["capacity"]["status"] != "critical")
    assert payload["provider_backoff"] == []

    # A queued job whose own retry delay has not elapsed is a provider backoff,
    # and it is reported per provider.
    ledger = RunLedger(config.ledger_path)
    ledger.enqueue_job({"job_id": "backoff-1", "dataset_id": "provider_bars",
                        "provider": "fixture", "symbol": "UI_TEST", "timeframe": "1d",
                        "start": "2026-09-14T00:00:00Z", "end": "2026-09-14T01:00:00Z",
                        "run_scope": "production"})
    claim = ledger.claim_next_job()
    ledger.fail_job(claim["job_id"], claim["run_id"], "transient", retryable=True,
                    delay_seconds=600.0)
    payload = client.get("/api/v1/operations/scheduler", headers=auth()).json()["data"]
    assert [item["provider"] for item in payload["queue_backoff"]] == ["fixture"]
    assert payload["queue_backoff"][0]["waiting"] == 1
    # The governed backoff is durable state, not a queue measurement.
    assert payload["provider_backoff"] == []
    RunLedger(config.ledger_path).record_provider_backoff(
        "fixture", until=datetime.now(timezone.utc) + timedelta(hours=1), failures=2,
        reason="provider_transient_failures")
    governed = client.get("/api/v1/operations/scheduler", headers=auth()).json()["data"]["provider_backoff"]
    assert [(item["provider"], item["failures"]) for item in governed] == [("fixture", 2)]


def test_global_pause_action_stops_dispatch_and_is_audited(client, config):
    create_plan(client, "p1", desired_state="enabled")
    ledger = RunLedger(config.ledger_path)
    # Make the plan genuinely due so the pause, not the schedule, is what holds it.
    ledger.set_task_next_run_at(task_id="p1", next_run_at="2026-09-14T11:00:00+00:00")
    paused = client.post("/api/v1/operations/scheduler/actions",
                         json={"command": "pause_dispatch"}, headers=auth())
    assert paused.status_code == 200
    assert paused.json()["data"]["dispatch_enabled"] is False
    assert ledger.dispatch_enabled() is False
    assert next(iter(ledger.write_audit_entries()))["action"] == "scheduler.pause_dispatch"
    # A scheduler tick that tries to dispatch while paused creates nothing.
    from datetime import datetime, timezone

    from data_center.scheduler import Scheduler
    result = Scheduler(ledger, instance_id="one", dispatch_enabled=True).tick(
        now=datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc))
    assert [item.get("reason") for item in result["decisions"]] == ["global_pause"]
    assert ledger.list_production_executions("p1") == []
    resumed = client.post("/api/v1/operations/scheduler/actions",
                          json={"command": "resume_dispatch"}, headers=auth())
    assert resumed.json()["data"]["dispatch_enabled"] is True
    claimed = Scheduler(ledger, instance_id="one", dispatch_enabled=True).tick(
        now=datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc))
    assert claimed["decisions"][0]["action"] == "execution_claimed"


def test_operations_scheduler_action_rejects_an_unknown_command(client):
    response = client.post("/api/v1/operations/scheduler/actions",
                           json={"command": "delete_everything"}, headers=auth())
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "unsupported_command"
    unauthenticated = client.post("/api/v1/operations/scheduler/actions",
                                  json={"command": "pause_dispatch"})
    assert unauthenticated.status_code == 401


def test_execution_retry_returns_a_linked_follow_up_round(client, config):
    """The retry route re-plans a failed round and reports the link (spec 8)."""
    create_plan(client, "p1", desired_state="enabled")
    started = client.post("/api/v1/production/tasks/p1/actions", json={"command": "run_now"},
                          headers=auth())
    execution_id = started.json()["data"]["execution_id"]
    ledger = RunLedger(config.ledger_path)
    # Plan the round (the API has no scheduler running) and fail its first run,
    # which is what a provider failure would leave behind.
    service = ProductionTasks(ledger, canonical_root=config.canonical_root)
    service.dispatch(task=ledger.get_production_task("p1"),
                     execution=ledger.get_production_execution(execution_id), step_budget=1)
    claim = ledger.claim_next_job()
    assert claim is not None
    ledger.fail_job(claim["job_id"], claim["run_id"], "provider exploded",
                    error_type="ProviderError", retryable=False)
    ledger.refresh_execution_steps(execution_id)
    ledger.close_finished_executions()
    assert ledger.get_production_execution(execution_id)["state"] == "failed"

    response = client.post(f"/api/v1/production/executions/{execution_id}/retry", headers=auth())
    assert response.status_code == 202, response.text
    payload = response.json()["data"]
    assert payload["retry_of_execution_id"] == execution_id
    assert payload["planned_steps"] >= 1
    follow_up = ledger.get_production_execution(payload["execution_id"])
    assert follow_up["trigger_source"] == "retry"
    assert follow_up["retry_of_execution_id"] == execution_id
    # The refusal path stays explicit: retrying an unknown round is a 404.
    missing = client.post("/api/v1/production/executions/absent/retry", headers=auth())
    assert missing.status_code == 404


def test_execution_history_paginates_with_a_cursor_bound_to_the_plan(client, config):
    create_plan(client, "p1", desired_state="enabled")
    create_plan(client, "p2", symbol="GBPUSD", bar_timeframes=[], desired_state="enabled")
    ledger = RunLedger(config.ledger_path)
    for _ in range(3):
        created = client.post("/api/v1/production/tasks/p1/actions", json={"command": "run_now"},
                              headers=auth()).json()["data"]
        # A plan may only have one non-terminal round, so each one is closed
        # before the next is triggered.
        ledger.finish_production_execution(created["execution_id"], state="completed", outcome="pass")
    first = client.get("/api/v1/production/tasks/p1/executions",
                       params={"page_size": 2}, headers=auth())
    page = first.json()["meta"]["page"]
    assert len(first.json()["data"]) == 2 and page["next_cursor"]
    seen = {item["execution_id"] for item in first.json()["data"]}
    second = client.get("/api/v1/production/tasks/p1/executions",
                        params={"page_size": 2, "cursor": page["next_cursor"]}, headers=auth())
    assert seen.isdisjoint({item["execution_id"] for item in second.json()["data"]})
    # A cursor is bound to the plan it was issued for.
    mismatched = client.get("/api/v1/production/tasks/p2/executions",
                            params={"page_size": 2, "cursor": page["next_cursor"]}, headers=auth())
    assert mismatched.status_code == 422
    assert mismatched.json()["errors"][0]["code"] == "cursor_error"
    # The history keeps the trigger source and the configuration version.
    assert {item["trigger_source"] for item in first.json()["data"]} == {"manual"}
    assert all(item["definition_version"] == 1 for item in first.json()["data"])
