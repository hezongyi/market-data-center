"""API surface policy: every mutating route must be classified, not memorized.

Adding a write endpoint must force two decisions that are otherwise easy to
forget: does it require the API key, and does it leave a write-audit record.
The inventory below is the single place those decisions live, and the first
test fails when a route is added or removed without updating it.
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from data_center.api.app import create_app
from data_center.runs.ledger import RunLedger
from data_center.settings import Settings

START = "2026-01-01T00:00:00Z"
END = "2026-01-02T00:00:00Z"
KEY = "surface-contract-key"

# (method, path template) -> (requires_api_key, writes_audit)
# ``POST /maintenance/plans`` is the only mutating verb that needs neither: it
# is a side-effect-free validation preview, so it queues nothing and audits
# nothing.  ``retry``, ``acknowledge`` and ``ingest/runs`` mutate the ledger or
# queue work but predate the audit trail; they are listed explicitly so the
# decision stays visible instead of implied.
MUTATING_ROUTES: dict[tuple[str, str], tuple[bool, bool]] = {
    ("POST", "/api/v1/maintenance/plans"): (False, False),
    ("POST", "/api/v1/maintenance/tasks"): (True, True),
    ("POST", "/api/v1/runs/{run_id}/retry"): (True, False),
    ("POST", "/api/v1/runs/{run_id}/acknowledge"): (True, False),
    ("POST", "/api/v1/ingest/runs"): (True, False),
    ("POST", "/api/v1/derive/runs"): (True, True),
    ("POST", "/api/v1/quality/checks"): (True, True),
    ("POST", "/api/v1/quality/findings/{finding_id}/state"): (True, True),
    ("POST", "/api/v1/economic/ingest"): (True, True),
}

INGEST_BODY = {"job_id": "surface", "provider": "fixture", "symbol": "UI_TEST", "asset_class": "test",
               "timeframe": "1d", "start": START, "end": END, "run_scope": "acceptance"}
TASK_BODY = {"run_kind": "ingest", "run_scope": "acceptance", "provider": "fixture", "symbol": "UI_TEST",
             "asset_class": "test", "timeframe": "1d", "start": START, "end": END}


def settings(tmp_path) -> Settings:
    return Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                    evidence_root=tmp_path / "evidence", api_key=KEY)


def requests_for(route: tuple[str, str]) -> tuple[str, dict, dict]:
    """Return (path, json body, query params) that reach the route handler."""
    method, template = route
    path = template.replace("{run_id}", "absent-run").replace("{finding_id}", "absent-finding")
    body: dict = {}
    params: dict = {}
    if template == "/api/v1/ingest/runs" or template == "/api/v1/quality/checks":
        body = INGEST_BODY
    elif template == "/api/v1/maintenance/tasks" or template == "/api/v1/maintenance/plans":
        body = TASK_BODY
    elif template == "/api/v1/derive/runs":
        body = {"job_id": "surface-derive", "provider": "fixture", "symbol": "UI_TEST",
                "recipe_id": "utc-24x7-1m-to-1h-ohlcv", "recipe_version": "1", "start": START, "end": END,
                "run_scope": "acceptance"}
    elif template == "/api/v1/quality/findings/{finding_id}/state":
        body = {"state": "acknowledged"}
    elif template == "/api/v1/economic/ingest":
        params = {"series_id": "PAYEMS", "run_scope": "acceptance"}
    assert method == "POST"
    return path, body, params


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
    requires_key, _ = MUTATING_ROUTES[route]
    if not requires_key:
        pytest.skip("route is classified as unauthenticated by design")
    client = TestClient(create_app(settings(tmp_path)))
    path, body, params = requests_for(route)
    missing = client.post(path, json=body or None, params=params)
    assert missing.status_code == 401, f"{route} accepted a request without a key"
    assert missing.json()["errors"][0] == {"code": "unauthorized", "message": "invalid api key"}
    wrong = client.post(path, json=body or None, params=params, headers={"X-API-Key": "not-the-key"})
    assert wrong.status_code == 401, f"{route} accepted an incorrect key"


def test_unauthenticated_routes_are_read_only(tmp_path) -> None:
    """The single exception must stay free of side effects: a preview queues nothing."""
    client = TestClient(create_app(settings(tmp_path)))
    response = client.post("/api/v1/maintenance/plans", json=TASK_BODY)
    assert response.status_code == 200
    assert response.json()["data"]["submittable"] is True
    assert client.get("/api/v1/runs").json()["data"] == []
    assert RunLedger(tmp_path / "runs.sqlite").write_audit_entries() == []


@pytest.mark.parametrize("route", sorted(route for route, policy in MUTATING_ROUTES.items() if policy[1]))
def test_auditing_routes_append_to_the_write_audit(route, tmp_path) -> None:
    config = settings(tmp_path)
    ledger = RunLedger(config.ledger_path)
    if route == ("POST", "/api/v1/quality/findings/{finding_id}/state"):
        ledger.record_findings([{"dataset_id": "provider_bars", "severity": "error",
                                 "code": "ohlc_inconsistent", "bar_ts": START}])
    client = TestClient(create_app(config))
    path, body, params = requests_for(route)
    if "absent-finding" in path:
        path = path.replace("absent-finding", ledger.findings()[0]["finding_id"])
    before = len(ledger.write_audit_entries())
    response = client.post(path, json=body or None, params=params, headers={"X-API-Key": KEY})
    assert response.status_code in {200, 202, 422}, f"{route} returned {response.status_code}"
    entries = ledger.write_audit_entries()
    assert len(entries) == before + 1, f"{route} did not append a write-audit entry"
    assert entries[0]["action"].startswith("maintenance.") or entries[0]["action"] == "quality.finding_state"


def test_audit_actor_never_contains_the_credential(tmp_path) -> None:
    config = settings(tmp_path)
    client = TestClient(create_app(config))
    client.post("/api/v1/maintenance/tasks", json=TASK_BODY, headers={"X-API-Key": KEY})
    entry = RunLedger(config.ledger_path).write_audit_entries()[0]
    assert KEY not in str(entry)
    assert entry["actor"].startswith("api-key:")


def test_read_routes_never_write_to_the_ledger(tmp_path) -> None:
    """A read must never mutate: the detail projection is compared to the stored receipt."""
    config = settings(tmp_path)
    ledger = RunLedger(config.ledger_path)
    run_id = ledger.enqueue_job({"job_id": "surface-read", "dataset_id": "provider_bars",
                                 "provider": "fixture", "symbol": "UI_TEST", "run_kind": "ingest",
                                 "run_scope": "acceptance", "start": START, "end": END})
    client = TestClient(create_app(config))
    stored = ledger.get(run_id)
    client.get("/api/v1/runs")
    client.get(f"/api/v1/runs/{run_id}")
    client.get(f"/api/v1/runs/{run_id}/detail")
    client.get("/api/v1/operations/queue")
    client.get("/api/v1/capabilities")
    assert ledger.get(run_id) == stored
    assert RunLedger(config.ledger_path).write_audit_entries() == []


def test_health_ready_keeps_its_status_codes(tmp_path) -> None:
    """The readiness route intentionally answers 200/503 with the same envelope shape."""
    client = TestClient(create_app(settings(tmp_path)))
    response = client.get("/api/v1/health/ready")
    assert response.status_code in {200, 503}
    payload = response.json()
    assert set(payload) == {"data", "meta", "errors"}
    assert payload["meta"]["schema_version"] == "v1"
    assert payload["errors"] == []


def test_envelope_helper_is_the_only_success_shape(tmp_path) -> None:
    """Success payloads carry request_id and schema_version without per-route copies."""
    client = TestClient(create_app(settings(tmp_path)))
    for path in ("/api/v1/health/live", "/api/v1/datasets", "/api/v1/capabilities"):
        payload = client.get(path, headers={"X-Request-ID": "trace-envelope"}).json()
        assert set(payload) == {"data", "meta", "errors"}
        assert payload["meta"] == {"request_id": "trace-envelope", "schema_version": "v1"}
        assert payload["errors"] == []


def test_queued_run_records_the_submitted_selector_and_window(tmp_path) -> None:
    """A queued run records the selector and window the operator submitted."""
    config = settings(tmp_path)
    client = TestClient(create_app(config))
    envelope = client.post("/api/v1/maintenance/tasks", json=TASK_BODY,
                           headers={"X-API-Key": KEY}).json()["data"]
    run = RunLedger(config.ledger_path).get(envelope["run_id"])
    assert run["timeframe"] == "1d" and run["asset_class"] == "test"
    # Pydantic serializes UTC instants with a Z suffix; compare instants, not spellings.
    instant = datetime.fromisoformat(START.replace("Z", "+00:00"))
    assert datetime.fromisoformat(run["start"].replace("Z", "+00:00")) == instant
    assert datetime.fromisoformat(run["end"].replace("Z", "+00:00")) == datetime.fromisoformat(
        END.replace("Z", "+00:00"))
    assert run["status"] == "queued" and run["created_at"]
    assert run["run_scope"] == "acceptance"
    assert run["run_kind"] == "ingest"
    assert datetime.fromisoformat(run["created_at"]).tzinfo == timezone.utc
