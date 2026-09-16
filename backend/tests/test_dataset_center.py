from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from data_center.api.app import create_app
from data_center.catalog.manifest import build_manifest, write_manifest
from data_center.dataset_center import (
    DatasetCenter,
    DatasetMember,
    managed_dataset_root,
)
from data_center.domain.models import ProviderBar
from data_center.settings import Settings
from data_center.storage.parquet import write_provider_bars
from data_center.storage.query import query_provider_bars


def test_dataset_member_inheritance_and_idempotent_request(tmp_path):
    center = DatasetCenter(tmp_path)
    item = center.create(dataset_id="eurusd", name="EURUSD dataset", notes="first")
    center.add_member("eurusd", DatasetMember(symbol="EURUSD"))
    assert item.provider == "dukascopy" and item.base_timeframe == "1m"
    first = center.request("eurusd", symbol="EURUSD", start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z", idempotency_key="k1")
    second = center.request("eurusd", symbol="EURUSD", start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z", idempotency_key="k1")
    assert first == second and first["status"] == "queued"
    with pytest.raises(ValueError, match="another request"):
        center.request("eurusd", symbol="EURUSD", start="2026-01-03T00:00:00Z", end="2026-01-04T00:00:00Z", idempotency_key="k1")


def test_paused_allows_manual_request_and_archived_rejects(tmp_path):
    center = DatasetCenter(tmp_path)
    center.create(dataset_id="eurusd", name="EURUSD")
    center.add_member("eurusd", DatasetMember(symbol="EURUSD"))
    center.update("eurusd", status="paused")
    assert center.request("eurusd", symbol="EURUSD", start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z")["status"] == "queued"
    center.update("eurusd", status="archived")
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
    assert client.post("/api/v1/managed-datasets/d/members", json={"symbol": "EURUSD"}, headers=headers).status_code == 201
    response = client.post("/api/v1/managed-datasets/d/maintenance", json={
        "symbol": "EURUSD", "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z"}, headers=headers)
    assert response.status_code == 202
    payload = response.json()["data"]
    assert payload["execution_id"] and payload["state"] == "pending"
    assert payload["dataset_id"] == "d"
    requests = client.get("/api/v1/managed-datasets/d/maintenance").json()["data"]
    assert requests[0]["execution"]["execution_id"] == payload["execution_id"]


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
