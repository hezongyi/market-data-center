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

