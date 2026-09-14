"""Contract tests for the unified maintenance workbench (v0.4 Phase 1)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from storage_fixtures import write_provider_bars

from data_center.api.app import create_app
from data_center.capacity import FixedCapacityPolicy
from data_center.ingest.worker import LocalWorker
from data_center.instants import parse_instant
from data_center.runs.ledger import RunLedger
from data_center.settings import Settings

START = "2026-01-01T00:00:00Z"
END = "2026-01-03T00:00:00Z"


def settings(tmp_path, **overrides) -> Settings:
    base = {"canonical_root": tmp_path / "lake", "ledger_path": tmp_path / "runs.sqlite",
            "evidence_root": tmp_path / "evidence"}
    return Settings(**{**base, **overrides})


def client(tmp_path, **overrides) -> TestClient:
    return TestClient(create_app(settings(tmp_path, **overrides)))


def ingest_request(**overrides) -> dict:
    payload = {"run_kind": "ingest", "run_scope": "acceptance", "provider": "fixture", "symbol": "UI_TEST",
               "asset_class": "test", "timeframe": "1d", "start": START, "end": END}
    payload.update(overrides)
    return payload


# -- planning preview ----------------------------------------------------


def test_plan_preview_describes_windows_without_queueing(tmp_path) -> None:
    http = client(tmp_path)
    response = http.post("/api/v1/maintenance/plans", json=ingest_request())
    assert response.status_code == 200
    preview = response.json()["data"]
    assert preview["validation"]["status"] == "valid"
    assert preview["submittable"] is True
    assert preview["plan"]["window_count"] >= 1
    assert preview["plan"]["semantics"] == "half-open"
    assert preview["capacity"]["status"] in {"ok", "warning", "critical"}
    assert preview["task"]["dataset_id"] == "provider_bars"
    # A preview must never create work.
    assert http.get("/api/v1/runs").json()["data"] == []


def test_plan_preview_reports_every_validation_error(tmp_path) -> None:
    http = client(tmp_path)
    preview = http.post("/api/v1/maintenance/plans", json={
        "run_kind": "ingest", "provider": "fixture", "timeframe": "1d",
        "start": END, "end": START,
    }).json()["data"]
    codes = {error["code"] for error in preview["validation"]["errors"]}
    assert "invalid_time_range" in codes
    assert "symbol_required" in codes
    assert preview["submittable"] is False
    assert preview["validation"]["status"] == "invalid"


def test_plan_preview_blocks_unknown_provider_in_production_scope(tmp_path) -> None:
    preview = client(tmp_path).post("/api/v1/maintenance/plans", json=ingest_request(
        provider="not_registered", run_scope="production", symbol="ANY", asset_class="crypto",
    )).json()["data"]
    assert preview["submittable"] is False
    assert {error["code"] for error in preview["validation"]["errors"]} == {"unsupported_provider"}


def test_gap_repair_without_a_gap_is_blocked_instead_of_reingesting(tmp_path) -> None:
    root = tmp_path / "lake"
    from data_center.domain.models import ProviderBar

    rows = [ProviderBar(symbol="UI_TEST", asset_class="test", provider="fixture", timeframe="1d",
                        bar_ts=datetime(2026, 1, day, tzinfo=timezone.utc), open=1, high=2, low=0, close=1,
                        volume=1, ingest_ts=datetime(2026, 1, 5, tzinfo=timezone.utc), source_hash=f"h{day}")
            for day in (1, 2)]
    write_provider_bars(root, rows, part_id="complete")
    http = client(tmp_path)
    preview = http.post("/api/v1/maintenance/plans", json=ingest_request(
        run_kind="gap_repair", start="2026-01-01T00:00:00Z", end="2026-01-03T00:00:00Z",
    )).json()["data"]
    assert preview["coverage"]["readiness_status"] == "ready"
    assert {error["code"] for error in preview["validation"]["errors"]} == {"no_gap_detected"}
    assert http.post("/api/v1/maintenance/tasks", json=ingest_request(run_kind="gap_repair")).status_code == 422


def test_derive_plan_requires_a_registered_recipe_and_input_snapshot(tmp_path) -> None:
    http = client(tmp_path)
    missing = http.post("/api/v1/maintenance/plans", json=ingest_request(run_kind="derive")).json()["data"]
    assert {error["code"] for error in missing["validation"]["errors"]} == {"recipe_required"}

    unknown = http.post("/api/v1/maintenance/plans", json=ingest_request(
        run_kind="derive", recipe_id="missing-recipe", recipe_version="1",
    )).json()["data"]
    assert {error["code"] for error in unknown["validation"]["errors"]} == {"recipe_not_registered"}

    empty = http.post("/api/v1/maintenance/plans", json=ingest_request(
        run_kind="derive", recipe_id="utc-24x7-1m-to-1d-ohlcv", recipe_version="1", timeframe="1m",
    )).json()["data"]
    assert "input_snapshot_empty" in {error["code"] for error in empty["validation"]["errors"]}


def test_maintenance_plan_rejects_run_kind_dataset_mismatch(tmp_path) -> None:
    http = client(tmp_path)
    invalid = [
        ingest_request(run_kind="derive", dataset_id="provider_bars"),
        ingest_request(run_kind="ingest", dataset_id="market_bars",
                       recipe_id="utc-24x7-1m-to-1d-ohlcv", recipe_version="1"),
        ingest_request(run_kind="derive", dataset_id="economic_observations", series_id="GDP"),
    ]
    for payload in invalid:
        preview = http.post("/api/v1/maintenance/plans", json=payload)
        assert preview.status_code == 200
        errors = preview.json()["data"]["validation"]["errors"]
        assert any(error["code"] == "unsupported_run_kind" for error in errors)
        assert preview.json()["data"]["submittable"] is False
        assert http.post("/api/v1/maintenance/tasks", json=payload).status_code == 422


# -- submission ----------------------------------------------------------


def test_maintenance_submission_returns_the_unified_queued_envelope(tmp_path) -> None:
    http = client(tmp_path)
    response = http.post("/api/v1/maintenance/tasks", json=ingest_request())
    assert response.status_code == 202
    envelope = response.json()["data"]
    assert envelope["status"] == "queued" and envelope["state"] == "queued"
    assert envelope["dataset_id"] == "provider_bars"
    assert envelope["run_kind"] == "ingest" and envelope["run_scope"] == "acceptance"
    assert envelope["run_ids"] == [envelope["run_id"]]
    assert envelope["window_count"] == len(envelope["run_ids"]) >= 1
    assert envelope["plan_id"] and envelope["submitted_at"] and envelope["audit_id"]
    assert envelope["selector"]["symbol"] == "UI_TEST"
    queued = http.get("/api/v1/runs").json()["data"]
    assert [run["run_id"] for run in queued] == envelope["run_ids"]
    assert all(run["status"] == "queued" for run in queued)


def test_maintenance_submission_requires_the_api_key(tmp_path) -> None:
    http = client(tmp_path, api_key="secret")
    assert http.post("/api/v1/maintenance/tasks", json=ingest_request()).status_code == 401
    authorized = http.post("/api/v1/maintenance/tasks", json=ingest_request(),
                           headers={"X-API-Key": "secret"})
    assert authorized.status_code == 202


def test_capacity_critical_protects_writes_and_is_audited(tmp_path) -> None:
    config = settings(tmp_path, capacity_fixed_free_ratio=0.01)
    http = TestClient(create_app(config))
    preview = http.post("/api/v1/maintenance/plans", json=ingest_request()).json()["data"]
    assert preview["capacity"]["status"] == "critical"
    assert preview["write_status"] == "protected" and preview["submittable"] is False
    assert preview["capacity"]["protected_reason"]["code"] == "capacity_critical"

    blocked = http.post("/api/v1/maintenance/tasks", json=ingest_request())
    assert blocked.status_code == 507
    assert blocked.json()["errors"][0]["code"] == "capacity_protected"
    assert blocked.json()["data"]["write_status"] == "protected"
    assert http.get("/api/v1/runs").json()["data"] == []
    audit = http.get("/api/v1/operations/audit").json()["data"]
    assert audit[0]["outcome"] == "protected"
    assert audit[0]["code"] == "capacity_protected"


def test_backfill_over_31_days_is_blocked_while_capacity_is_warning(tmp_path) -> None:
    config = settings(tmp_path, capacity_fixed_free_ratio=0.03,
                      capacity_warning_free_ratio=0.05, capacity_critical_free_ratio=0.02)
    http = TestClient(create_app(config))
    long_backfill = ingest_request(run_kind="backfill", start="2026-01-01T00:00:00Z",
                                   end="2026-06-01T00:00:00Z", run_scope="production")
    preview = http.post("/api/v1/maintenance/plans", json=long_backfill).json()["data"]
    assert preview["capacity"]["requested_days"] > 31
    assert preview["capacity"]["protected_reason"]["code"] == "capacity_warning_backfill"
    assert http.post("/api/v1/maintenance/tasks", json=long_backfill).status_code == 507
    short_backfill = {**long_backfill, "end": "2026-01-20T00:00:00Z"}
    assert http.post("/api/v1/maintenance/tasks", json=short_backfill).status_code == 202


def test_write_audit_records_the_actor_without_storing_the_credential(tmp_path) -> None:
    http = client(tmp_path, api_key="secret")
    http.post("/api/v1/maintenance/tasks", json=ingest_request(),
              headers={"X-API-Key": "secret", "X-Operator": "ops-console"})
    entry = http.get("/api/v1/operations/audit").json()["data"][0]
    assert entry["actor"] == "ops-console"
    assert entry["action"] == "maintenance.ingest" and entry["outcome"] == "queued"
    assert entry["selector"]["symbol"] == "UI_TEST"
    assert (parse_instant(entry["time_range"]["start"])
            == datetime.fromisoformat(START.replace("Z", "+00:00")))
    assert "secret" not in str(entry)

    anonymous = client(tmp_path / "other", api_key="secret")
    anonymous.post("/api/v1/maintenance/tasks", json=ingest_request(), headers={"X-API-Key": "secret"})
    actor = anonymous.get("/api/v1/operations/audit").json()["data"][0]["actor"]
    assert actor.startswith("api-key:") and "secret" not in actor


def test_legacy_write_endpoints_share_the_unified_envelope(tmp_path) -> None:
    http = client(tmp_path)
    quality = http.post("/api/v1/quality/checks", json={
        "job_id": "contract-quality", "provider": "fixture", "symbol": "UI_TEST", "asset_class": "test",
        "timeframe": "1d", "start": START, "end": END, "run_scope": "acceptance",
    })
    assert quality.status_code == 202
    assert quality.json()["data"]["run_kind"] == "quality"
    assert quality.json()["data"]["status"] == "queued"


def test_quality_run_executes_as_a_verification_and_records_findings(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "runs.sqlite")
    worker = LocalWorker(tmp_path / "lake", ledger)
    run_id = ledger.enqueue_job({"job_id": "quality-1", "dataset_id": "provider_bars", "provider": "fixture",
                                 "symbol": "UI_TEST", "asset_class": "test", "timeframe": "1d",
                                 "run_kind": "quality", "run_scope": "acceptance",
                                 "start": START, "end": END})
    assert worker.run_next() is True
    receipt = ledger.get(run_id)
    assert receipt["status"] == "pass"
    assert receipt["verification"]["publishes_parts"] is False
    assert receipt["run_kind"] == "quality"
    # A verification run publishes no canonical part, so it cannot claim a manifest.
    assert "manifest" not in receipt


def test_verification_findings_are_persisted_with_run_linkage(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "runs.sqlite")
    worker = LocalWorker(tmp_path / "lake", ledger)
    # The controlled fixture provider emits one bar per day, so a 1m quality
    # check over the same range observes a real coverage gap.
    job = {"job_id": "quality-gap", "dataset_id": "provider_bars", "provider": "fixture",
           "symbol": "UI_TEST", "asset_class": "test", "timeframe": "1m",
           "run_kind": "quality", "run_scope": "acceptance", "start": START, "end": END}
    run_id = ledger.enqueue_job(job)
    worker.run_next()
    findings = ledger.findings()
    assert findings, "a coverage gap must be recorded as a finding"
    assert all(item["run_id"] == run_id for item in findings)
    assert all(item["finding_id"].startswith("finding-") for item in findings)
    assert all(item["severity"] == "warning" for item in findings)
    # A degraded dataset is still a successful verification, not a failed run.
    receipt = ledger.get(run_id)
    assert receipt["status"] == "pass"
    assert receipt["quality_summary"]["status"] == "pass"

    # Re-observing the same defect keeps one identity and counts occurrences.
    second = ledger.enqueue_job({**job, "job_id": "quality-gap-2"})
    worker.run_next()
    refreshed = {item["finding_id"]: item for item in ledger.findings()}
    assert len(refreshed) == len(findings)
    assert all(item["occurrence_count"] >= 2 for item in refreshed.values())
    assert all(item["last_run_id"] == second for item in refreshed.values())


def test_quality_run_failure_keeps_the_terminal_receipt_immutable(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "runs.sqlite")
    worker = LocalWorker(tmp_path / "lake", ledger)
    run_id = ledger.enqueue_job({"job_id": "quality-invalid", "dataset_id": "provider_bars",
                                 "provider": "acceptance_invalid", "symbol": "UI_TEST", "asset_class": "test",
                                 "timeframe": "1d", "run_kind": "quality", "run_scope": "acceptance",
                                 "start": START, "end": END})
    worker.run_next()
    receipt = ledger.get(run_id)
    assert receipt["status"] == "failed"
    assert receipt["failure_stage"] == "execute"
    with pytest.raises(ValueError):
        ledger.put(run_id, {**receipt, "status": "pass"})


def seed_one_minute_bars(root, *, symbol: str = "UI_TEST", hours: int = 24) -> None:
    """Publish an immutable raw 1m part so derived work has a real input."""
    from datetime import timedelta

    from data_center.domain.models import ProviderBar

    start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    rows = [ProviderBar(symbol=symbol, asset_class="test", provider="fixture", timeframe="1m",
                        bar_ts=start + timedelta(minutes=index), open=100 + index, high=101 + index,
                        low=99 + index, close=100.5 + index, volume=10 + index,
                        ingest_ts=start, source_hash=f"seed-{index}")
            for index in range(hours * 60)]
    from storage_fixtures import write_provider_bars as publish_provider_bars

    publish_provider_bars(root, rows, part_id="seed-1m")


def test_parity_task_queues_a_verification_run_instead_of_a_derive(tmp_path) -> None:
    root = tmp_path / "lake"
    seed_one_minute_bars(root)
    http = client(tmp_path)
    request = ingest_request(run_kind="parity", recipe_id="utc-24x7-1m-to-1h-ohlcv", recipe_version="1",
                             start="2026-03-01T00:00:00Z", end="2026-03-02T00:00:00Z")
    response = http.post("/api/v1/maintenance/tasks", json=request)
    assert response.status_code == 202, response.json()
    envelope = response.json()["data"]
    assert envelope["dataset_id"] == "market_bars"
    assert envelope["input_snapshot_id"]
    run = http.get(f"/api/v1/runs/{envelope['run_id']}").json()["data"]
    assert run["run_kind"] == "parity"
    assert run["status"] == "queued"

    worker = LocalWorker(root, RunLedger(tmp_path / "runs.sqlite"))
    assert worker.run_next() is True
    receipt = http.get(f"/api/v1/runs/{envelope['run_id']}").json()["data"]
    assert receipt["verification"]["publishes_parts"] is False
    assert receipt["run_kind"] == "parity"


def test_derive_task_queues_a_derive_run_with_its_input_snapshot(tmp_path) -> None:
    root = tmp_path / "lake"
    seed_one_minute_bars(root)
    http = client(tmp_path)
    response = http.post("/api/v1/maintenance/tasks", json=ingest_request(
        run_kind="derive", recipe_id="utc-24x7-1m-to-1h-ohlcv", recipe_version="1",
        start="2026-03-01T00:00:00Z", end="2026-03-02T00:00:00Z",
    ))
    assert response.status_code == 202, response.json()
    envelope = response.json()["data"]
    assert envelope["input_snapshot_id"]
    run = http.get(f"/api/v1/runs/{envelope['run_id']}").json()["data"]
    assert run["run_kind"] == "derive"
    assert run["input_snapshot_id"] == envelope["input_snapshot_id"]

    worker = LocalWorker(root, RunLedger(tmp_path / "runs.sqlite"))
    assert worker.run_next() is True
    receipt = http.get(f"/api/v1/runs/{envelope['run_id']}").json()["data"]
    assert receipt["status"] == "pass"
    assert receipt["row_count"] == 24
    detail = http.get(f"/api/v1/runs/{envelope['run_id']}/detail").json()["data"]
    assert detail["manifest_status"] == "published"
    assert detail["selector"]["recipe_id"] == "utc-24x7-1m-to-1h-ohlcv"


# -- run read models -----------------------------------------------------


def test_run_detail_projects_stage_windows_and_retry_chain(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "runs.sqlite")
    worker = LocalWorker(tmp_path / "lake", ledger)
    run_id = ledger.enqueue_job({
        "job_id": "detail-1", "dataset_id": "provider_bars", "provider": "fixture", "symbol": "UI_TEST",
        "asset_class": "test", "timeframe": "1d", "run_kind": "ingest", "run_scope": "acceptance",
        "start": START, "end": END,
        "execution_plan": {"run_kind": "ingest", "run_scope": "acceptance", "dataset_id": "provider_bars",
                           "selector": {"provider": "fixture", "symbol": "UI_TEST", "timeframe": "1d"},
                           # A serialized plan carries an explicit offset; Python
                           # 3.10 does not parse the "Z" suffix in fromisoformat.
                           "windows": [{"ordinal": 0, "start": "2026-01-01T00:00:00+00:00",
                                        "end": "2026-01-03T00:00:00+00:00", "reason": "ingest",
                                        "semantics": "half-open"}]},
    })
    worker.run_next()
    http = TestClient(create_app(settings(tmp_path)))
    detail = http.get(f"/api/v1/runs/{run_id}/detail").json()["data"]
    assert detail["stage"] == "published"
    assert detail["outcome"] == "pass" and detail["terminal"] is True
    assert detail["window_count"] == 1
    assert detail["manifest_status"] == "published"
    assert detail["selector"] == {"provider": "fixture", "symbol": "UI_TEST", "timeframe": "1d"}
    assert detail["time_range"]["semantics"] == "half-open"
    assert [item["relation"] for item in detail["retry_chain"]] == ["origin"]
    # The raw receipt stays byte-identical to the stored run.
    assert http.get(f"/api/v1/runs/{run_id}").json()["data"] == ledger.get(run_id)

    failed = RunLedger(tmp_path / "second.sqlite")
    worker2 = LocalWorker(tmp_path / "lake2", failed)
    job = {"job_id": "detail-2", "dataset_id": "provider_bars", "provider": "acceptance_invalid",
           "symbol": "UI_TEST", "asset_class": "test", "timeframe": "1d", "run_kind": "ingest",
           "run_scope": "acceptance", "start": START, "end": END}
    failed_id = failed.enqueue_job(job)
    for _ in range(3):
        worker2.run_next()
    retried = failed.retry_run(failed_id)
    http2 = TestClient(create_app(Settings(canonical_root=tmp_path / "lake2",
                                           ledger_path=tmp_path / "second.sqlite",
                                           evidence_root=tmp_path / "evidence2")))
    chain = http2.get(f"/api/v1/runs/{failed_id}/detail").json()["data"]["retry_chain"]
    assert [item["run_id"] for item in chain] == [failed_id, retried]
    assert [item["relation"] for item in chain] == ["origin", "retry"]


def test_missing_run_detail_returns_404(tmp_path) -> None:
    assert client(tmp_path).get("/api/v1/runs/absent/detail").status_code == 404


def test_run_list_filters_and_cursor_pagination(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "runs.sqlite")
    created = []
    for index in range(3):
        created.append(ledger.enqueue_job({
            "job_id": f"page-{index}", "dataset_id": "provider_bars" if index != 1 else "market_bars",
            "provider": "fixture", "symbol": "UI_TEST", "run_kind": "ingest" if index != 2 else "derive",
            "run_scope": "acceptance", "start": START, "end": END}))
    http = TestClient(create_app(settings(tmp_path)))
    everything = http.get("/api/v1/runs").json()
    assert len(everything["data"]) == 3
    assert everything["meta"]["page"]["paginated"] is False

    filtered = http.get("/api/v1/runs", params={"dataset_id": "provider_bars"}).json()
    assert {run["run_id"] for run in filtered["data"]} == {created[0], created[2]}
    assert http.get("/api/v1/runs", params={"run_kind": "derive"}).json()["meta"]["count"] == 1
    assert http.get("/api/v1/runs", params={"run_scope": "production"}).json()["meta"]["count"] == 0

    first = http.get("/api/v1/runs", params={"page_size": 2}).json()
    assert len(first["data"]) == 2 and first["meta"]["page"]["has_more"] is True
    cursor = first["meta"]["page"]["next_cursor"]
    second = http.get("/api/v1/runs", params={"page_size": 2, "cursor": cursor}).json()
    assert len(second["data"]) == 1 and second["meta"]["page"]["has_more"] is False
    assert {run["run_id"] for run in first["data"]} | {run["run_id"] for run in second["data"]} == set(created)

    mismatched = http.get("/api/v1/runs", params={"page_size": 2, "cursor": cursor,
                                                  "dataset_id": "market_bars"})
    assert mismatched.status_code == 422
    assert mismatched.json()["errors"][0]["code"] == "cursor_error"
    assert http.get("/api/v1/runs", params={"page_size": 0}).status_code == 422


def test_run_list_supports_a_created_time_window(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "runs.sqlite")
    ledger.enqueue_job({"job_id": "window", "dataset_id": "provider_bars", "provider": "fixture",
                        "symbol": "UI_TEST", "run_kind": "ingest", "run_scope": "acceptance",
                        "start": START, "end": END})
    http = TestClient(create_app(settings(tmp_path)))
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert http.get("/api/v1/runs", params={"created_from": future}).json()["meta"]["count"] == 0
    assert http.get("/api/v1/runs", params={"created_to": future}).json()["meta"]["count"] == 1
    assert http.get("/api/v1/runs", params={"created_from": "not-a-time"}).status_code == 422


# -- findings ------------------------------------------------------------


def test_findings_support_structured_filters_and_state_transitions(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "runs.sqlite")
    stored = ledger.record_findings([
        {"dataset_id": "provider_bars", "run_id": "run-1", "severity": "error", "code": "ohlc_inconsistent",
         "bar_ts": "2026-01-01T00:00:00+00:00", "message": "high below open"},
        {"dataset_id": "market_bars", "run_id": "run-2", "severity": "warning", "code": "coverage_degraded",
         "bar_ts": "2026-01-02T00:00:00+00:00", "message": "missing bucket"},
    ])
    assert len(stored) == 2
    http = TestClient(create_app(settings(tmp_path)))
    assert http.get("/api/v1/quality/findings").json()["meta"]["state_counts"] == {"open": 2}
    assert http.get("/api/v1/quality/findings", params={"severity": "error"}).json()["meta"]["count"] == 1
    assert http.get("/api/v1/quality/findings", params={"dataset_id": "market_bars"}).json()["meta"]["count"] == 1
    assert http.get("/api/v1/quality/findings", params={"run_id": "run-1"}).json()["meta"]["count"] == 1

    finding_id = stored[0]["finding_id"]
    denied = http.post(f"/api/v1/quality/findings/{finding_id}/state", json={"state": "acknowledged"})
    assert denied.status_code == 200
    assert http.get("/api/v1/quality/findings", params={"state": "acknowledged"}).json()["meta"]["count"] == 1
    resolved = http.post(f"/api/v1/quality/findings/{finding_id}/state",
                         json={"state": "resolved", "resolved_by_run_id": "run-9"}).json()["data"]
    assert resolved["state"] == "resolved" and resolved["resolved_by_run_id"] == "run-9"
    assert http.post(f"/api/v1/quality/findings/{finding_id}/state", json={"state": "nonsense"}).status_code == 422
    assert http.post("/api/v1/quality/findings/absent/state", json={"state": "open"}).status_code == 404


def test_findings_require_the_api_key_for_state_changes(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "runs.sqlite")
    stored = ledger.record_findings([{"dataset_id": "provider_bars", "severity": "error",
                                      "code": "ohlc_inconsistent", "bar_ts": "2026-01-01T00:00:00+00:00"}])
    http = TestClient(create_app(settings(tmp_path, api_key="secret")))
    path = f"/api/v1/quality/findings/{stored[0]['finding_id']}/state"
    assert http.post(path, json={"state": "acknowledged"}).status_code == 401
    assert http.post(path, json={"state": "acknowledged"}, headers={"X-API-Key": "secret"}).status_code == 200


def test_receipts_surface_under_the_actions_that_were_recorded(tmp_path) -> None:
    """Every highlighted action must be one the platform writes, and vice versa."""
    config = settings(tmp_path)
    from data_center.evidence import operation_receipt, write_receipt

    for action in ("deployment_activate", "capacity_check"):
        write_receipt(config.evidence_root, operation_receipt(
            action=action, command=f"{action} probe", started_at="2026-09-13T00:00:00+00:00",
            result="pass", details={"deployment_id": "probe-release"}))
    http = TestClient(create_app(config))
    payload = http.get("/api/v1/operations/receipts").json()["data"]
    assert payload["available"] is True
    for action in ("deployment_activate", "capacity_check"):
        assert payload["latest"][action]["action"] == action
        assert payload["latest"][action]["result"] == "pass"
    assert payload["latest"]["deployment_activate"]["deployment_id"] == "probe-release"
    assert {item["action"] for item in payload["receipts"]} == {"deployment_activate", "capacity_check"}
    # A name the platform does not write must not be reported as a recorded action.
    assert "release" not in payload["latest"] and "deployment" not in payload["latest"]


# -- capabilities --------------------------------------------------------


def test_capabilities_read_model_exposes_providers_recipes_and_datasets(tmp_path) -> None:
    payload = client(tmp_path).get("/api/v1/capabilities").json()["data"]
    providers = {item["provider"]: item for item in payload["providers"]}
    assert {"fixture", "binance", "dukascopy", "yfinance"} <= set(providers)
    assert providers["dukascopy"]["maintenance_timeframes"] == ["1m"]
    assert any(item["symbol"] == "EURUSD" for item in providers["dukascopy"]["instruments"])
    recipes = {(item["recipe_id"], item["recipe_version"]) for item in payload["recipes"]}
    assert ("utc-24x7-1m-to-1d-ohlcv", "1") in recipes
    datasets = {item["dataset_id"]: item for item in payload["datasets"]}
    assert datasets["provider_bars"]["kind"] == "raw"
    assert datasets["market_bars"]["kind"] == "derived"
    assert payload["write_status"] in {"available", "protected"}
    kinds = {item["run_kind"]: item["datasets"] for item in payload["run_kinds"]}
    assert kinds["derive"] == ["market_bars"] and kinds["quality"] == ["provider_bars", "economic_observations"]


def test_market_bars_coverage_reports_recipe_semantics(tmp_path) -> None:
    from data_center.domain.models import MarketBar
    from data_center.storage.parquet import write_market_bars

    root = tmp_path / "lake"
    rows = [MarketBar(symbol="UI_TEST", asset_class="test", provider="fixture", timeframe="1d",
                      source_timeframe="1m", bar_ts=datetime(2026, 1, day, tzinfo=timezone.utc),
                      open=1, high=2, low=0, close=1, volume=1, price_basis="raw", session_profile="utc_24x7",
                      recipe_id="utc-24x7-1m-to-1d-ohlcv", recipe_version="1", input_snapshot_id="snap-1",
                      ingest_ts=datetime(2026, 1, 5, tzinfo=timezone.utc), source_hash=f"h{day}")
            for day in (1, 2)]
    paths = write_market_bars(root, rows, part_id="derived")
    from data_center.catalog.manifest import build_manifest, write_manifest

    write_manifest(root, build_manifest(
        root, run_id="derived", dataset_id="market_bars", schema_version="market_bars.v1", paths=paths,
        row_count=len(rows), quality_summary={"status": "pass", "finding_count": 0, "findings": []},
        lineage={"input_kind": "snapshot", "input_dataset": "provider_bars", "source_timeframe": "1m",
                 "target_timeframe": "1d", "input_hash": "in", "output_hash": "out",
                 "recipe_id": "utc-24x7-1m-to-1d-ohlcv", "recipe_version": "1",
                 "input_snapshot_id": "snap-1", "source_hash_digest": "digest"}))
    payload = TestClient(create_app(settings(tmp_path))).get("/api/v1/market-bars/coverage", params={
        "provider": "fixture", "symbol": "UI_TEST", "timeframe": "1d", "price_basis": "raw",
        "recipe_id": "utc-24x7-1m-to-1d-ohlcv", "recipe_version": "1",
    }).json()["data"]
    assert payload["row_count"] == 2
    assert payload["recipe_status"] == "registered"
    assert payload["recipe"]["target_timeframe"] == "1d"
    assert payload["input_snapshot_ids"] == ["snap-1"]


def test_capacity_history_and_queue_views_are_read_only(tmp_path) -> None:
    http = client(tmp_path)
    history = http.get("/api/v1/operations/capacity-history").json()["data"]
    assert set(history["live"]) >= {"status", "free_ratio", "warning_free_ratio", "critical_free_ratio"}
    assert history["live"]["fixed_measurement"] is False
    assert history["events"] == [] and history["recorded_only"] is True
    queue = http.get("/api/v1/operations/queue").json()["data"]
    assert queue["queued"] == 0 and queue["runs_by_status"] == {}


def test_a_deployment_manifest_may_not_pin_a_fake_capacity_measurement(tmp_path) -> None:
    manifest = tmp_path / "deployment.json"
    manifest.write_text("{}")
    config = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                      evidence_root=tmp_path / "evidence", capacity_fixed_free_ratio=0.03,
                      deployment_manifest=manifest)
    with pytest.raises(ValueError):
        config.capacity_policy()
    assert FixedCapacityPolicy.for_free_ratio(0.03).inspect(tmp_path).status == "critical"


# -- operations read models ----------------------------------------------


def test_worker_and_receipt_views_are_read_only(tmp_path) -> None:
    config = settings(tmp_path)
    from data_center.observability import AlertSink

    ledger = RunLedger(config.ledger_path)
    http = TestClient(create_app(config))
    worker = http.get("/api/v1/operations/worker").json()["data"]
    assert worker["heartbeat_status"] == "unknown" and worker["running_jobs"] == []
    assert worker["queue"]["queued"] == 0

    ledger.heartbeat()
    heartbeat = http.get("/api/v1/operations/worker").json()["data"]
    assert heartbeat["heartbeat_status"] == "fresh"
    assert heartbeat["heartbeat_age_seconds"] < 60

    receipts = http.get("/api/v1/operations/receipts").json()["data"]
    assert set(receipts) >= {"available", "receipts", "latest"}
    if receipts["available"]:
        assert isinstance(receipts["receipts"], list)

    # Capacity history only reports transitions the monitor recorded.
    sink = AlertSink(config.evidence_root / "alerts", True)
    sink.emit_transition("capacity", "warning", event="capacity_warning",
                         fields={"status": "warning", "free_ratio": 0.03,
                                 "warning_free_ratio": 0.05, "critical_free_ratio": 0.02})
    history = http.get("/api/v1/operations/capacity-history").json()["data"]
    assert history["recorded_only"] is True
    assert history["event_count"] == 1
    assert history["events"][0]["event"] == "capacity_warning"
    assert history["live"]["status"] in {"ok", "warning", "critical"}
    assert "fixed_measurement" in history["live"]
