from datetime import datetime, timezone

from fastapi.testclient import TestClient

from data_center.api.app import create_app
from data_center.settings import Settings


def test_runs_and_quality_contract(tmp_path) -> None:
    app = create_app(Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite"))
    client = TestClient(app)
    job = {"job_id": "test-1", "symbol": "BTCUSDT", "start": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(), "end": datetime(2026, 1, 2, tzinfo=timezone.utc).isoformat()}
    response = client.post("/api/v1/quality/checks", json=job)
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "pass"


def test_write_api_requires_key(tmp_path) -> None:
    app = create_app(Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite", api_key="secret"))
    client = TestClient(app)
    job = {"job_id": "secure", "symbol": "BTCUSDT", "start": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(), "end": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()}
    unauthorized = client.post("/api/v1/ingest/runs", json=job)
    assert unauthorized.status_code == 401
    assert unauthorized.json()["errors"] == [{"code": "401", "message": "invalid api key"}]
    assert client.post("/api/v1/ingest/runs", json=job, headers={"X-API-Key": "secret"}).status_code == 200


def test_api_can_serve_built_webui(tmp_path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>data center</html>", encoding="utf-8")
    app = create_app(Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite", webui_dist=dist))
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "data center" in response.text
