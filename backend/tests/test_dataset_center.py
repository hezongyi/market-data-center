from data_center.dataset_center import DatasetCenter, DatasetMember
from data_center.api.app import create_app
from data_center.settings import Settings
from fastapi.testclient import TestClient


def test_dataset_member_inheritance_and_idempotent_request(tmp_path):
    center = DatasetCenter(tmp_path)
    item = center.create(dataset_id="eurusd", name="EURUSD dataset", notes="first")
    center.add_member("eurusd", DatasetMember(symbol="EURUSD"))
    assert item.provider == "dukascopy" and item.base_timeframe == "1m"
    first = center.request("eurusd", symbol="EURUSD", start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z", idempotency_key="k1")
    second = center.request("eurusd", symbol="EURUSD", start="x", end="y", idempotency_key="k1")
    assert first == second and first["status"] == "queued"


def test_paused_allows_manual_request_and_archived_rejects(tmp_path):
    center = DatasetCenter(tmp_path)
    center.create(dataset_id="eurusd", name="EURUSD")
    center.add_member("eurusd", DatasetMember(symbol="EURUSD"))
    center.update("eurusd", status="paused")
    assert center.request("eurusd", symbol="EURUSD", start="a", end="b")["status"] == "queued"
    center.update("eurusd", status="archived")
    try:
        center.request("eurusd", symbol="EURUSD", start="a", end="b")
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
    assert payload["run_ids"] and payload["run_kind"] == "backfill"
    assert payload["dataset_id"] == "provider_bars"
