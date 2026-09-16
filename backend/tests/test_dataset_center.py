import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from data_center.api.app import create_app
from data_center.catalog.manifest import build_manifest, write_manifest
from data_center.dataset_center import (
    DatasetCenter,
    DatasetMember,
    DatasetStateError,
    managed_dataset_root,
)
from data_center.domain.models import ProviderBar
from data_center.ingest.worker import LocalWorker
from data_center.production_tasks import ProductionTasks
from data_center.runs.ledger import RunLedger
from data_center.settings import Settings
from data_center.storage.parquet import write_provider_bars
from data_center.storage.query import query_market_bars, query_provider_bars


def test_dataset_member_inheritance_and_idempotent_request(tmp_path):
    center = DatasetCenter(tmp_path)
    item = center.create(dataset_id="eurusd", name="EURUSD dataset", notes="first")
    center.add_member("eurusd", DatasetMember(symbol="EURUSD"), expected_version=1)
    assert item.provider == "dukascopy" and item.base_timeframe == "1m"
    first = center.request("eurusd", symbol="EURUSD", start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z", idempotency_key="k1")
    second = center.request("eurusd", symbol="EURUSD", start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z", idempotency_key="k1")
    assert first == second and first["status"] == "queued"
    with pytest.raises(ValueError, match="another request"):
        center.request("eurusd", symbol="EURUSD", start="2026-01-03T00:00:00Z", end="2026-01-04T00:00:00Z", idempotency_key="k1")


def test_dataset_state_corruption_fails_closed_without_overwrite(tmp_path):
    center = DatasetCenter(tmp_path)
    center.create(dataset_id="safe", name="Safe")
    center.path.write_text("{truncated")

    with pytest.raises(DatasetStateError, match="unreadable"):
        center.list()
    assert center.path.read_text() == "{truncated"
    with pytest.raises(DatasetStateError, match="unreadable"):
        DatasetCenter(tmp_path)


def test_dataset_writers_reload_under_a_cross_process_lock(tmp_path):
    first = DatasetCenter(tmp_path)
    second = DatasetCenter(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda item: item[0].create(dataset_id=item[1], name=item[1]),
                      ((first, "one"), (second, "two"))))
    assert {item.dataset_id for item in DatasetCenter(tmp_path).list()} == {"one", "two"}


def test_dataset_updates_are_optimistic_and_only_real_changes_increment_version(tmp_path):
    center = DatasetCenter(tmp_path)
    center.create(dataset_id="eurusd", name="EURUSD")
    updated = center.update("eurusd", expected_version=1, notes="reviewed")
    assert updated.version == 2
    with pytest.raises(ValueError, match="version conflict"):
        center.update("eurusd", expected_version=1, notes="stale")
    with pytest.raises(ValueError, match="does not change"):
        center.update("eurusd", expected_version=2, notes="reviewed")
    with pytest.raises(ValueError, match="unknown dataset fields"):
        center.update("eurusd", expected_version=2, bogus="value")
    assert center.get("eurusd").version == 2


def test_empty_member_targets_are_an_explicit_override(tmp_path):
    center = DatasetCenter(tmp_path)
    center.create(dataset_id="eurusd", name="EURUSD")
    item = center.add_member(
        "eurusd", DatasetMember(symbol="eurusd", derived_targets=()), expected_version=1)
    member = item.as_dict()["members"]["EURUSD"]
    assert member["symbol"] == "EURUSD"
    assert member["derived_targets"] == []
    assert member["effective_derived_targets"] == []


def test_api_preserves_empty_targets_and_optimistic_dataset_versions(tmp_path):
    config = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                      evidence_root=tmp_path / "evidence", api_key="key")
    client = TestClient(create_app(config))
    auth = {"X-API-Key": "key"}
    assert client.post("/api/v1/managed-datasets", json={
        "dataset_id": "empty", "name": "Empty targets"}, headers=auth).status_code == 201
    member = client.post("/api/v1/managed-datasets/empty/members", json={
        "symbol": "eurusd", "derived_targets": [], "expected_version": 1}, headers=auth)
    assert member.status_code == 201
    assert member.json()["data"]["members"]["EURUSD"]["effective_derived_targets"] == []
    assert client.get("/api/v1/managed-datasets/empty/coverage").json()["data"]["derived"] == []
    assert client.get(
        "/api/v1/managed-datasets/empty/bars?symbol=eurusd&timeframe=5m").status_code == 422

    assert client.patch("/api/v1/managed-datasets/empty", json={
        "notes": "missing version"}, headers=auth).status_code == 409
    assert client.patch("/api/v1/managed-datasets/empty", json={
        "expected_version": 1, "notes": "stale"}, headers=auth).status_code == 409
    assert client.patch("/api/v1/managed-datasets/empty", json={
        "expected_version": 2, "bogus": "field"}, headers=auth).status_code == 409
    assert client.get("/api/v1/managed-datasets/empty").json()["data"]["version"] == 2


def test_paused_allows_manual_request_and_archived_rejects(tmp_path):
    center = DatasetCenter(tmp_path)
    center.create(dataset_id="eurusd", name="EURUSD")
    center.add_member("eurusd", DatasetMember(symbol="EURUSD"), expected_version=1)
    center.update("eurusd", status="paused", expected_version=2)
    assert center.request("eurusd", symbol="EURUSD", start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z")["status"] == "queued"
    center.update("eurusd", status="archived", expected_version=3)
    try:
        center.request("eurusd", symbol="EURUSD", start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z")
    except ValueError as exc:
        assert "read-only" in str(exc)
    else:
        raise AssertionError("archived dataset accepted maintenance")


def test_managed_maintenance_uses_governed_ledger(tmp_path):
    config = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                      evidence_root=tmp_path / "evidence", api_key="key",
                      capacity_fixed_free_ratio=0.9)
    client = TestClient(create_app(config))
    headers = {"X-API-Key": "key", "Idempotency-Key": "managed-1"}
    assert client.post("/api/v1/managed-datasets", json={"dataset_id": "d", "name": "D"}, headers=headers).status_code == 201
    assert client.post("/api/v1/managed-datasets/d/members", json={
        "symbol": "EURUSD", "expected_version": 1}, headers=headers).status_code == 201
    response = client.post("/api/v1/managed-datasets/d/maintenance", json={
        "symbol": "EURUSD", "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"}, headers=headers)
    assert response.status_code == 202
    payload = response.json()["data"]
    assert payload["execution_id"] and payload["state"] == "pending"
    assert payload["dataset_id"] == "d"
    requests = client.get("/api/v1/managed-datasets/d/maintenance").json()["data"]
    assert requests[0]["execution"]["execution_id"] == payload["execution_id"]


def test_refused_second_maintenance_does_not_leave_an_orphan_request(tmp_path):
    config = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                      evidence_root=tmp_path / "evidence", api_key="key",
                      capacity_fixed_free_ratio=0.9)
    client = TestClient(create_app(config))
    auth = {"X-API-Key": "key"}
    assert client.post("/api/v1/managed-datasets", json={"dataset_id": "d", "name": "D"},
                       headers=auth).status_code == 201
    assert client.post("/api/v1/managed-datasets/d/members", json={
        "symbol": "EURUSD", "expected_version": 1},
                       headers=auth).status_code == 201

    first = client.post(
        "/api/v1/managed-datasets/d/maintenance",
        json={"symbol": "EURUSD", "start": "2026-01-01T00:00:00Z",
              "end": "2026-01-01T01:00:00Z"},
        headers={**auth, "Idempotency-Key": "first-window"},
    )
    assert first.status_code == 202
    assert client.patch("/api/v1/managed-datasets/d", json={
        "status": "paused", "expected_version": 2},
                        headers=auth).status_code == 200

    second = client.post(
        "/api/v1/managed-datasets/d/maintenance",
        json={"symbol": "EURUSD", "start": "2026-01-01T01:00:00Z",
              "end": "2026-01-01T02:00:00Z"},
        headers={**auth, "Idempotency-Key": "paused-window"},
    )

    assert second.status_code == 409
    requests = client.get("/api/v1/managed-datasets/d/maintenance").json()["data"]
    assert len(requests) == 1
    assert all(request.get("execution", {}).get("execution_id") for request in requests)


def test_managed_idempotency_keys_are_namespaced_by_dataset(tmp_path):
    config = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                      evidence_root=tmp_path / "evidence", api_key="key",
                      capacity_fixed_free_ratio=0.9)
    client = TestClient(create_app(config))
    auth = {"X-API-Key": "key"}
    for dataset_id in ("one", "two"):
        assert client.post("/api/v1/managed-datasets", json={
            "dataset_id": dataset_id, "name": dataset_id}, headers=auth).status_code == 201
        assert client.post(f"/api/v1/managed-datasets/{dataset_id}/members", json={
            "symbol": "EURUSD", "expected_version": 1}, headers=auth).status_code == 201
        response = client.post(
            f"/api/v1/managed-datasets/{dataset_id}/maintenance",
            json={"symbol": "EURUSD", "start": "2026-01-01T00:00:00Z",
                  "end": "2026-01-01T01:00:00Z"},
            headers={**auth, "Idempotency-Key": "same"})
        assert response.status_code == 202, response.json()


def test_managed_idempotency_namespace_is_unambiguous_when_ids_contain_colons(tmp_path):
    config = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                      evidence_root=tmp_path / "evidence", api_key="key",
                      capacity_fixed_free_ratio=0.9)
    client = TestClient(create_app(config))
    auth = {"X-API-Key": "key"}
    requests = (("a", "b:k"), ("a:b", "k"))
    executions = []
    for dataset_id, idempotency_key in requests:
        assert client.post("/api/v1/managed-datasets", json={
            "dataset_id": dataset_id, "name": dataset_id}, headers=auth).status_code == 201
        assert client.post(f"/api/v1/managed-datasets/{dataset_id}/members", json={
            "symbol": "EURUSD", "expected_version": 1}, headers=auth).status_code == 201
        response = client.post(
            f"/api/v1/managed-datasets/{dataset_id}/maintenance",
            json={"symbol": "EURUSD", "start": "2026-01-01T00:00:00Z",
                  "end": "2026-01-01T01:00:00Z"},
            headers={**auth, "Idempotency-Key": idempotency_key})
        assert response.status_code == 202, response.json()
        executions.append(response.json()["data"])

    assert [execution["dataset_id"] for execution in executions] == ["a", "a:b"]
    assert executions[0]["execution_id"] != executions[1]["execution_id"]
    for dataset_id, _ in requests:
        listed = client.get(f"/api/v1/managed-datasets/{dataset_id}/maintenance").json()["data"]
        assert len(listed) == 1
        assert listed[0]["dataset_id"] == dataset_id


def test_legacy_unattached_request_replays_original_production_action_key(tmp_path):
    lake = tmp_path / "lake"
    config = Settings(canonical_root=lake, ledger_path=tmp_path / "runs.sqlite",
                      evidence_root=tmp_path / "evidence", api_key="key",
                      capacity_fixed_free_ratio=0.9)
    center = DatasetCenter(lake)
    item = center.create(dataset_id="legacy", name="Legacy")
    center.add_member("legacy", DatasetMember(symbol="EURUSD"), expected_version=1)
    request_key = "old:key"
    value, _ = center.submit_request(
        "legacy", symbol="EURUSD", start="2026-01-01T00:00:00Z",
        end="2026-01-01T01:00:00Z", idempotency_key=request_key)
    state = json.loads(center.path.read_text())
    legacy_value = dict(state["requests"][0])
    legacy_value.pop("action_key_version")
    state["requests"] = {f"legacy:{request_key}": legacy_value}
    center.path.write_text(json.dumps(state))

    task_id = f"managed-legacy-{value['request_id']}"
    ProductionTasks(RunLedger(config.ledger_path), canonical_root=lake).create(
        task_id=task_id, name=f"{item.name} · {value['start']} → {value['end']}",
        desired_state="enabled", actor="legacy-test", request_id=value["request_id"],
        idempotency_key=f"legacy:{request_key}:create",
        definition={
            "managed_dataset_id": "legacy", "provider": "dukascopy", "symbol": "EURUSD",
            "raw_timeframe": "1m", "price_basis": "bid", "bar_timeframes": ["5m"],
            "window_policy": {"mode": "fixed", "start": value["start"], "end": value["end"]},
            "schedule": {"schedule": "manual"},
        })

    response = TestClient(create_app(config)).post(
        "/api/v1/managed-datasets/legacy/maintenance",
        json={"symbol": "EURUSD", "start": value["start"], "end": value["end"]},
        headers={"X-API-Key": "key", "X-Operator": "legacy-test",
                 "Idempotency-Key": request_key},
    )

    assert response.status_code == 202, response.json()
    assert response.json()["data"]["task_id"] == task_id
    assert DatasetCenter(lake).requests("legacy")[0]["execution"]["execution_id"]


def test_archived_or_unknown_managed_dataset_cannot_use_generic_ingest(tmp_path):
    config = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                      evidence_root=tmp_path / "evidence", api_key="key",
                      capacity_fixed_free_ratio=0.9)
    client = TestClient(create_app(config))
    auth = {"X-API-Key": "key"}
    assert client.post("/api/v1/managed-datasets", json={
        "dataset_id": "arch", "name": "Archived"}, headers=auth).status_code == 201
    assert client.post("/api/v1/managed-datasets/arch/members", json={
        "symbol": "EURUSD", "expected_version": 1}, headers=auth).status_code == 201
    job = {"job_id": "bypass", "managed_dataset_id": "arch", "provider": "dukascopy",
           "symbol": "EURUSD", "asset_class": "crypto", "timeframe": "1m",
           "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"}
    assert client.post("/api/v1/ingest/runs", json=job, headers=auth).status_code == 409
    job["asset_class"] = "fx"
    assert client.patch("/api/v1/managed-datasets/arch", json={
        "status": "archived", "expected_version": 2}, headers=auth).status_code == 200
    assert client.post("/api/v1/ingest/runs", json=job, headers=auth).status_code == 409
    job["managed_dataset_id"] = "missing"
    assert client.post("/api/v1/ingest/runs", json=job, headers=auth).status_code == 404
    assert RunLedger(config.ledger_path).list() == []


def test_production_task_cannot_bypass_managed_dataset_definition(tmp_path):
    config = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                      evidence_root=tmp_path / "evidence", api_key="key",
                      capacity_fixed_free_ratio=0.9)
    client = TestClient(create_app(config))
    auth = {"X-API-Key": "key"}
    assert client.post("/api/v1/managed-datasets", json={
        "dataset_id": "managed", "name": "Managed"}, headers=auth).status_code == 201
    assert client.post("/api/v1/managed-datasets/managed/members", json={
        "symbol": "EURUSD", "expected_version": 1}, headers=auth).status_code == 201
    definition = {
        "managed_dataset_id": "managed", "provider": "dukascopy", "symbol": "GBPUSD",
        "raw_timeframe": "1m", "price_basis": "bid", "bar_timeframes": ["5m"],
        "window_policy": {"mode": "fixed", "start": "2026-01-01T00:00:00Z",
                          "end": "2026-01-01T01:00:00Z"},
        "schedule": {"schedule": "manual"},
    }
    assert client.post("/api/v1/production/plans", json={
        "definition": definition}).status_code == 409
    assert client.post("/api/v1/production/tasks", json={
        "task_id": "bad-member", "name": "bad", "definition": definition},
        headers=auth).status_code == 409

    definition["symbol"] = "EURUSD"
    created = client.post("/api/v1/production/tasks", json={
        "task_id": "valid-managed", "name": "valid", "definition": definition}, headers=auth)
    assert created.status_code == 201, created.json()
    assert client.post("/api/v1/production/tasks/valid-managed/actions", json={
        "command": "resume"}, headers=auth).status_code == 200
    triggered = client.post("/api/v1/production/tasks/valid-managed/actions", json={
        "command": "run_now"}, headers=auth)
    assert triggered.status_code == 200, triggered.json()
    execution_id = triggered.json()["data"]["execution_id"]
    assert client.patch("/api/v1/managed-datasets/managed", json={
        "status": "archived", "expected_version": 2}, headers=auth).status_code == 200
    assert client.post("/api/v1/production/tasks/valid-managed/actions", json={
        "command": "run_now"}, headers=auth).status_code == 409
    assert client.post(f"/api/v1/production/executions/{execution_id}/retry",
                       headers=auth).status_code == 409


def test_direct_managed_derive_uses_scoped_snapshot_and_publication(tmp_path):
    lake = tmp_path / "lake"
    config = Settings(canonical_root=lake, ledger_path=tmp_path / "runs.sqlite",
                      evidence_root=tmp_path / "evidence", api_key="key",
                      capacity_fixed_free_ratio=0.9)
    client = TestClient(create_app(config))
    auth = {"X-API-Key": "key"}
    assert client.post("/api/v1/managed-datasets", json={
        "dataset_id": "managed", "name": "Managed"}, headers=auth).status_code == 201
    assert client.post("/api/v1/managed-datasets/managed/members", json={
        "symbol": "EURUSD", "expected_version": 1}, headers=auth).status_code == 201
    scoped = managed_dataset_root(lake, "managed")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [ProviderBar(
        managed_dataset_id="managed", symbol="EURUSD", asset_class="fx",
        provider="dukascopy", timeframe="1m", bar_ts=start + timedelta(minutes=index),
        open=1 + index / 10_000, high=1.1 + index / 10_000,
        low=0.9 + index / 10_000, close=1.05 + index / 10_000,
        volume=1, price_type="bid", ingest_ts=start + timedelta(hours=1),
        source_hash=f"managed-{index}",
    ) for index in range(60)]
    paths = write_provider_bars(scoped, rows, part_id="managed-raw")
    write_manifest(scoped, build_manifest(
        scoped, run_id="managed-raw", dataset_id="provider_bars",
        schema_version="provider_bars.v1", paths=paths, row_count=60,
        quality_summary={"status": "pass", "finding_count": 0, "findings": []}))
    assert query_provider_bars(lake, provider="dukascopy", symbol="EURUSD", timeframe="1m") == []

    response = client.post("/api/v1/derive/runs", headers=auth, json={
        "job_id": "direct-managed", "managed_dataset_id": "managed",
        "provider": "dukascopy", "symbol": "EURUSD",
        "recipe_id": "utc-24x7-1m-to-5m-ohlcv", "recipe_version": "1",
        "start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(),
        "run_scope": "maintenance", "run_kind": "derive",
    })
    assert response.status_code == 202, response.json()
    run_id = response.json()["data"]["run_id"]
    ledger = RunLedger(config.ledger_path)
    assert ledger.get(run_id)["managed_dataset_id"] == "managed"
    assert LocalWorker(lake, ledger).run_next() is True
    assert ledger.get(run_id)["status"] == "pass"
    assert len(query_market_bars(
        scoped, provider="dukascopy", symbol="EURUSD", timeframe="5m",
        price_basis="bid", recipe_id="utc-24x7-1m-to-5m-ohlcv",
        recipe_version="1")) == 12
    assert query_market_bars(
        lake, provider="dukascopy", symbol="EURUSD", timeframe="5m",
        price_basis="bid", recipe_id="utc-24x7-1m-to-5m-ohlcv",
        recipe_version="1") == []

    raw_first = client.get(
        "/api/v1/managed-datasets/managed/bars",
        params={"symbol": "EURUSD", "timeframe": "1m", "page_size": 10},
    ).json()
    raw_second = client.get(
        "/api/v1/managed-datasets/managed/bars",
        params={"symbol": "EURUSD", "timeframe": "1m", "page_size": 10,
                "cursor": raw_first["meta"]["next_cursor"]},
    ).json()
    assert len(raw_first["data"]) == len(raw_second["data"]) == 10
    assert raw_first["data"][-1]["bar_ts"] < raw_second["data"][0]["bar_ts"]

    derived_first = client.get(
        "/api/v1/managed-datasets/managed/bars",
        params={"symbol": "EURUSD", "timeframe": "5m", "page_size": 2},
    ).json()
    derived_second = client.get(
        "/api/v1/managed-datasets/managed/bars",
        params={"symbol": "EURUSD", "timeframe": "5m", "page_size": 2,
                "cursor": derived_first["meta"]["next_cursor"]},
    ).json()
    assert len(derived_first["data"]) == len(derived_second["data"]) == 2
    assert derived_first["data"][-1]["bar_ts"] < derived_second["data"][0]["bar_ts"]

    assert client.post("/api/v1/managed-datasets", json={
        "dataset_id": "other", "name": "Other"}, headers=auth).status_code == 201
    assert client.post("/api/v1/managed-datasets/other/members", json={
        "symbol": "EURUSD", "expected_version": 1}, headers=auth).status_code == 201
    cross_dataset = client.get(
        "/api/v1/managed-datasets/other/bars",
        params={"symbol": "EURUSD", "timeframe": "1m", "page_size": 10,
                "cursor": raw_first["meta"]["next_cursor"]},
    )
    assert cross_dataset.status_code == 422
    assert cross_dataset.json()["errors"][0]["code"] == "cursor_error"


def test_generic_retry_rejects_archived_managed_dataset_without_new_run(tmp_path):
    config = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                      evidence_root=tmp_path / "evidence", api_key="key",
                      capacity_fixed_free_ratio=0.9)
    client = TestClient(create_app(config))
    auth = {"X-API-Key": "key"}
    assert client.post("/api/v1/managed-datasets", json={
        "dataset_id": "managed", "name": "Managed"}, headers=auth).status_code == 201
    assert client.post("/api/v1/managed-datasets/managed/members", json={
        "symbol": "EURUSD", "expected_version": 1}, headers=auth).status_code == 201
    ledger = RunLedger(config.ledger_path)
    run_id = ledger.enqueue_job({
        "job_id": "failed-managed", "managed_dataset_id": "managed",
        "dataset_id": "provider_bars", "provider": "dukascopy", "symbol": "EURUSD",
        "asset_class": "fx", "timeframe": "1m", "price_basis": "bid",
        "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z",
    })
    claimed = ledger.claim_next_job()
    ledger.fail_job(claimed["job_id"], run_id, "failed", retryable=False)
    first_retry = client.post(f"/api/v1/runs/{run_id}/retry", headers=auth)
    assert first_retry.status_code == 202
    retry_id = first_retry.json()["data"]["run_id"]
    assert ledger.get(retry_id)["managed_dataset_id"] == "managed"
    retry_claim = ledger.claim_next_job()
    assert retry_claim["run_id"] == retry_id
    ledger.fail_job(retry_claim["job_id"], retry_id, "failed again", retryable=False)
    assert client.patch("/api/v1/managed-datasets/managed", json={
        "status": "archived", "expected_version": 2}, headers=auth).status_code == 200
    before = {run["run_id"] for run in ledger.list()}

    response = client.post(f"/api/v1/runs/{retry_id}/retry", headers=auth)

    assert response.status_code == 409
    assert {run["run_id"] for run in ledger.list()} == before


def test_managed_canonical_root_never_falls_back_to_global_data(tmp_path):
    root = managed_dataset_root(tmp_path, "d")
    bar = ProviderBar(symbol="EURUSD", asset_class="fx", provider="dukascopy", timeframe="1m",
                      bar_ts=datetime(2026, 1, 1, tzinfo=timezone.utc), open=1, high=1,
                      low=1, close=1, volume=1, price_type="bid",
                      ingest_ts=datetime.now(timezone.utc), source_hash="h",
                      managed_dataset_id="d")
    paths = write_provider_bars(root, [bar], part_id="managed")
    write_manifest(root, build_manifest(root, run_id="managed", dataset_id="provider_bars",
                                        schema_version="provider_bars.v1", paths=paths, row_count=1,
                                        quality_summary={"status": "pass", "finding_count": 0,
                                                         "findings": []}))
    assert query_provider_bars(root, provider="dukascopy", symbol="EURUSD", timeframe="1m")
    assert query_provider_bars(tmp_path, provider="dukascopy", symbol="EURUSD", timeframe="1m") == []
