"""HTTP contract for production plans: preview, create, change, list and history.

The routes are one adapter over the production task module, so these tests check
the externally visible promises: a preview that queues nothing, field-level
validation errors, stable conflict codes, idempotent writes, version conflicts
and cursor pagination bound to its filters (AC01, AC07, AC16).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from data_center.api.app import create_app
from data_center.runs.ledger import RunLedger
from data_center.settings import Settings

KEY = "production-api-key"
NOW = "2026-09-14T12:00:00+00:00"


def definition(**overrides) -> dict:
    base = {
        "provider": "dukascopy", "symbol": "EURUSD", "raw_timeframe": "1m", "price_basis": "bid",
        "bar_timeframes": ["5m"],
        "window_policy": {"mode": "continuous", "history_start": "2026-01-01T00:00:00+00:00"},
        "schedule": {"schedule": "fixed_rate", "interval_seconds": 900, "anchor": NOW},
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
    body = {"task_id": task_id, "name": overrides.pop("name", task_id),
            "definition": definition(**overrides)}
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
    assert preview["schedule"]["next_runs"][:2] == [NOW, "2026-09-14T12:15:00+00:00"]
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
    assert task["next_run_at"] == NOW
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


def test_capabilities_route_still_answers_with_a_plan_aware_payload(client):
    response = client.get("/api/v1/capabilities", headers=auth())
    assert response.status_code == 200
    assert response.json()["data"]["providers"]
