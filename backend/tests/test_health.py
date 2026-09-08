from fastapi.testclient import TestClient

from data_center.api.app import create_app


def test_health_contract() -> None:
    response = TestClient(create_app()).get("/api/v1/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["status"] == "ok"
    assert payload["meta"]["schema_version"] == "v1"

