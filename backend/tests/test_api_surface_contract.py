"""API surface policy: every mutating route must be classified, not memorized.

Adding a write endpoint must force two decisions that are otherwise easy to
forget: does it require the API key, and does it leave a write-audit record.
The inventory below is the single place those decisions live — together with
the request that reaches each handler — and the inventory test fails when a
route is added or removed without updating it.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from data_center.api.app import create_app
from data_center.instants import parse_instant
from data_center.runs.ledger import RunLedger
from data_center.settings import Settings

START = "2026-01-01T00:00:00Z"
END = "2026-01-02T00:00:00Z"
KEY = "surface-contract-key"

INGEST_BODY = {"job_id": "surface", "provider": "fixture", "symbol": "UI_TEST", "asset_class": "test",
               "timeframe": "1d", "start": START, "end": END, "run_scope": "acceptance"}
TASK_BODY = {"run_kind": "ingest", "run_scope": "acceptance", "provider": "fixture", "symbol": "UI_TEST",
             "asset_class": "test", "timeframe": "1d", "start": START, "end": END}
DERIVE_BODY = {"job_id": "surface-derive", "provider": "fixture", "symbol": "UI_TEST",
               "recipe_id": "utc-24x7-1m-to-1h-ohlcv", "recipe_version": "1", "start": START, "end": END,
               "run_scope": "acceptance"}
# A production plan is validated against the registry, so the surface fixture
# uses an approved instrument and a registered recipe chain rather than a
# synthetic symbol: an invalid definition is expected to be refused, not stored.
PLAN_BODY = {
    "name": "surface",
    "desired_state": "paused",
    "definition": {
        "provider": "dukascopy", "symbol": "EURUSD", "raw_timeframe": "1m", "price_basis": "bid",
        "bar_timeframes": ["5m"],
        "window_policy": {"mode": "continuous", "history_start": START},
        "schedule": {"schedule": "manual"},
    },
}


@dataclass(frozen=True)
class RoutePolicy:
    """What the platform promises for one mutating route."""

    requires_key: bool
    writes_audit: bool
    body: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    audit_action: str | None = None
    # Status the parametrized audit test expects for this fixture request; a
    # refusal is a legitimate outcome as long as it is audited.
    expect_status: int = 202
    note: str = ""


# ``POST /maintenance/plans`` is the only mutating verb that needs neither: it
# is a side-effect-free validation preview, so it queues nothing and audits
# nothing.  ``retry``, ``acknowledge`` and ``ingest/runs`` mutate the ledger or
# queue work but predate the audit trail; they are listed explicitly so the
# decision stays visible instead of implied.
MUTATING_ROUTES: dict[tuple[str, str], RoutePolicy] = {
    ("POST", "/api/v1/auth/login"): RoutePolicy(False, False, {"username": "admin", "password": "bad"}, expect_status=401),
    ("POST", "/api/v1/auth/initialize"): RoutePolicy(False, False, {"username": "admin", "password": "short"}, expect_status=422),
    ("POST", "/api/v1/auth/logout"): RoutePolicy(False, False, expect_status=200),
    ("POST", "/api/v1/auth/change-password"): RoutePolicy(True, False, {"current_password": "bad", "new_password": "bad"}, expect_status=401),
    ("POST", "/api/v1/maintenance/plans"): RoutePolicy(
        False, False, TASK_BODY, note="side-effect-free validation preview"),
    ("POST", "/api/v1/maintenance/tasks"): RoutePolicy(True, True, TASK_BODY, audit_action="maintenance.ingest"),
    ("PATCH", "/api/v1/maintenance/tasks/{task_id}"): RoutePolicy(True, False, {"status": "paused"}, expect_status=404),
    ("POST", "/api/v1/runs/{run_id}/retry"): RoutePolicy(
        True, True, audit_action="runs.retry", expect_status=404),
    ("POST", "/api/v1/runs/{run_id}/acknowledge"): RoutePolicy(
        True, True, audit_action="runs.acknowledge", expect_status=404),
    ("POST", "/api/v1/ingest/runs"): RoutePolicy(
        True, True, INGEST_BODY, audit_action="maintenance.ingest", expect_status=200),
    ("POST", "/api/v1/derive/runs"): RoutePolicy(
        True, True, DERIVE_BODY, audit_action="maintenance.derive", expect_status=422),
    ("POST", "/api/v1/quality/checks"): RoutePolicy(True, True, INGEST_BODY, audit_action="maintenance.quality"),
    ("POST", "/api/v1/quality/findings/{finding_id}/state"): RoutePolicy(
        True, True, {"state": "acknowledged"}, audit_action="quality.finding_state", expect_status=200),
    ("POST", "/api/v1/economic/ingest"): RoutePolicy(
        True, True, params={"series_id": "PAYEMS", "run_scope": "acceptance"},
        audit_action="maintenance.ingest"),
    ("POST", "/api/v1/production/tasks"): RoutePolicy(
        True, True, PLAN_BODY, audit_action="production.task.create", expect_status=201),
    ("POST", "/api/v1/production/plans"): RoutePolicy(False, False, {"schedule": "manual"}, expect_status=200),
    ("POST", "/api/v1/production/tasks/{task_id}/actions"): RoutePolicy(
        True, True, {"command": "pause"}, audit_action="production.task.pause", expect_status=404),
    ("PATCH", "/api/v1/production/tasks/{task_id}"): RoutePolicy(
        True, False, {"desired_state": "paused"}, expect_status=404),
    ("POST", "/api/v1/production/executions/{execution_id}/retry"): RoutePolicy(
        True, True, audit_action="production.execution.retry", expect_status=404),
    ("POST", "/api/v1/operations/scheduler/actions"): RoutePolicy(
        True, True, {"command": "pause_dispatch"},
        audit_action="scheduler.pause_dispatch", expect_status=200),
    ("POST", "/api/v1/managed-datasets"): RoutePolicy(True, False, {"dataset_id": "x", "name": "x"}, expect_status=201),
    ("PATCH", "/api/v1/managed-datasets/{dataset_id}"): RoutePolicy(True, False, {"notes": "x"}, expect_status=404),
    ("POST", "/api/v1/managed-datasets/{dataset_id}/members"): RoutePolicy(True, False, {"symbol": "EURUSD"}, expect_status=404),
    ("POST", "/api/v1/managed-datasets/{dataset_id}/maintenance"): RoutePolicy(True, False, {"symbol": "EURUSD", "start": START, "end": END}, expect_status=404),
}

AUDITING_ROUTES = sorted(route for route, policy in MUTATING_ROUTES.items() if policy.writes_audit)


def settings(tmp_path) -> Settings:
    return Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                    evidence_root=tmp_path / "evidence", api_key=KEY)


def route_path(template: str, ledger: RunLedger | None = None) -> str:
    """Resolve a path template to a callable path.

    Authentication is decided before any lookup, so a placeholder id is enough
    for the credential tests; the audit tests pass a ledger with a real finding.
    """
    path = template.replace("{run_id}", "absent-run")
    if "{finding_id}" in template:
        finding = ledger.findings()[0] if ledger and ledger.findings() else None
        path = path.replace("{finding_id}", finding["finding_id"] if finding else "absent-finding")
    path = path.replace("{task_id}", "absent-task")
    return path


def test_every_mutating_route_is_classified(tmp_path) -> None:
    app = create_app(settings(tmp_path))
    actual = {(method, route.path) for route in app.routes
              for method in getattr(route, "methods", set()) or set()
              if method in {"POST", "PUT", "PATCH", "DELETE"}}
    assert actual == set(MUTATING_ROUTES), (
        "a mutating route was added or removed without deciding its authentication and audit policy; "
        f"unclassified: {sorted(actual - set(MUTATING_ROUTES))}, "
        f"stale: {sorted(set(MUTATING_ROUTES) - actual)}"
    )


@pytest.mark.parametrize("route", sorted(MUTATING_ROUTES))
def test_protected_routes_reject_missing_and_wrong_credentials(route, tmp_path) -> None:
    policy = MUTATING_ROUTES[route]
    if not policy.requires_key:
        pytest.skip("route is classified as unauthenticated by design")
    client = TestClient(create_app(settings(tmp_path)))
    path = route_path(route[1])
    request_method = getattr(client, route[0].lower())
    missing = request_method(path, json=policy.body or None, params=policy.params)
    assert missing.status_code == 401, f"{route} accepted a request without a key"
    assert missing.json()["errors"][0] == {"code": "unauthorized", "message": "invalid api key"}
    wrong = request_method(path, json=policy.body or None, params=policy.params,
                        headers={"X-API-Key": "not-the-key"})
    assert wrong.status_code == 401, f"{route} accepted an incorrect key"


def test_unauthenticated_routes_are_read_only(tmp_path) -> None:
    """The single exception must stay free of side effects: a preview queues nothing."""
    client = TestClient(create_app(settings(tmp_path)))
    response = client.post("/api/v1/maintenance/plans", json=TASK_BODY)
    assert response.status_code == 200
    assert response.json()["data"]["submittable"] is True
    assert client.get("/api/v1/runs").json()["data"] == []
    assert RunLedger(tmp_path / "runs.sqlite").write_audit_entries() == []


@pytest.mark.parametrize("route", AUDITING_ROUTES)
def test_auditing_routes_append_to_the_write_audit(route, tmp_path) -> None:
    policy = MUTATING_ROUTES[route]
    config = settings(tmp_path)
    ledger = RunLedger(config.ledger_path)
    if route == ("POST", "/api/v1/quality/findings/{finding_id}/state"):
        ledger.record_findings([{"dataset_id": "provider_bars", "severity": "error",
                                 "code": "ohlc_inconsistent", "bar_ts": START}])
    client = TestClient(create_app(config))
    before = len(ledger.write_audit_entries())
    response = client.post(route_path(route[1], ledger), json=policy.body or None,
                           params=policy.params, headers={"X-API-Key": KEY})
    assert response.status_code == policy.expect_status, (
        f"{route} returned {response.status_code}, expected {policy.expect_status}")
    entries = ledger.write_audit_entries()
    assert len(entries) == before + 1, f"{route} did not append a write-audit entry"
    assert entries[0]["action"] == policy.audit_action, (
        f"{route} audited as {entries[0]['action']!r}, expected {policy.audit_action!r}")


def test_audit_actor_never_contains_the_credential(tmp_path) -> None:
    config = settings(tmp_path)
    client = TestClient(create_app(config))
    client.post("/api/v1/maintenance/tasks", json=TASK_BODY, headers={"X-API-Key": KEY})
    entry = RunLedger(config.ledger_path).write_audit_entries()[0]
    assert KEY not in str(entry)
    assert entry["actor"].startswith("api-key:")


def test_read_routes_never_write_to_the_ledger(tmp_path) -> None:
    """A read must never mutate: the stored receipt is compared after each read."""
    config = settings(tmp_path)
    ledger = RunLedger(config.ledger_path)
    run_id = ledger.enqueue_job({"job_id": "surface-read", "dataset_id": "provider_bars",
                                 "provider": "fixture", "symbol": "UI_TEST", "run_kind": "ingest",
                                 "run_scope": "acceptance", "start": START, "end": END})
    client = TestClient(create_app(config))
    stored = ledger.get(run_id)
    for path in ("/api/v1/runs", f"/api/v1/runs/{run_id}", f"/api/v1/runs/{run_id}/detail",
                 "/api/v1/operations/queue", "/api/v1/capabilities"):
        client.get(path)
    assert ledger.get(run_id) == stored
    assert RunLedger(config.ledger_path).write_audit_entries() == []


def test_success_responses_share_one_envelope_shape(tmp_path) -> None:
    """Success payloads carry request_id and schema_version without per-route copies."""
    client = TestClient(create_app(settings(tmp_path)))
    for path in ("/api/v1/health", "/api/v1/health/live", "/api/v1/health/ready",
                 "/api/v1/datasets", "/api/v1/capabilities"):
        response = client.get(path, headers={"X-Request-ID": "trace-envelope"})
        assert response.status_code in {200, 503}, path
        payload = response.json()
        assert set(payload) == {"data", "meta", "errors"}, path
        assert payload["meta"] == {"request_id": "trace-envelope", "schema_version": "v1"}, path
        assert payload["errors"] == [], path


def test_unknown_dataset_returns_a_safe_planner_error(tmp_path) -> None:
    """An unsupported dataset must fail as a contract error, never as a raw pydantic dump."""
    client = TestClient(create_app(settings(tmp_path)))
    for path, body in (("/api/v1/quality/checks", {**INGEST_BODY, "dataset_id": "not_a_dataset"}),
                       ("/api/v1/maintenance/tasks", {**TASK_BODY, "dataset_id": "not_a_dataset"})):
        response = client.post(path, json=body, headers={"X-API-Key": KEY})
        assert response.status_code == 422, path
        error = response.json()["errors"][0]
        assert error["code"] == "invalid_request", path
        assert "not_a_dataset" in error["message"], path
        assert "supported datasets" in error["message"], path
        # A raw pydantic dump would leak internal model names and field paths.
        assert "validation error" not in error["message"].lower(), path
        assert "MaintenanceTaskRequest" not in error["message"], path


def test_queued_run_records_the_submitted_selector_and_window(tmp_path) -> None:
    """A queued run records the selector and window the operator submitted."""
    config = settings(tmp_path)
    client = TestClient(create_app(config))
    envelope = client.post("/api/v1/maintenance/tasks", json=TASK_BODY,
                           headers={"X-API-Key": KEY}).json()["data"]
    run = RunLedger(config.ledger_path).get(envelope["run_id"])
    assert run["timeframe"] == "1d" and run["asset_class"] == "test"
    # Pydantic serializes UTC instants with a Z suffix; compare instants, not spellings.
    assert datetime.fromisoformat(run["start"].replace("Z", "+00:00")) == datetime.fromisoformat(
        START.replace("Z", "+00:00"))
    assert datetime.fromisoformat(run["end"].replace("Z", "+00:00")) == datetime.fromisoformat(
        END.replace("Z", "+00:00"))
    assert run["status"] == "queued" and run["created_at"]
    assert run["run_scope"] == "acceptance"
    assert run["run_kind"] == "ingest"
    assert parse_instant(run["created_at"]).tzinfo == timezone.utc
