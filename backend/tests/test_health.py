from fastapi.testclient import TestClient

from data_center.api.app import create_app
from data_center.settings import Settings


def test_health_contract(tmp_path) -> None:
    settings = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "ledger.sqlite",
                        evidence_root=tmp_path / "evidence")
    response = TestClient(create_app(settings)).get("/api/v1/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["status"] == "ok"
    assert payload["meta"]["schema_version"] == "v1"
