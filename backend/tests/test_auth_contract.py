from fastapi.testclient import TestClient

from data_center.api.app import create_app
from data_center.settings import Settings


def test_auth_initialize_login_me_change_password(tmp_path):
    settings = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "ledger.sqlite",
                        evidence_root=tmp_path / "evidence", auth_cookie_secure=False)
    client = TestClient(create_app(settings))
    assert client.get("/api/v1/auth/me").status_code == 401
    initialized = client.post("/api/v1/auth/initialize", json={"username": "admin", "password": "initial-password-123"})
    assert initialized.status_code == 200
    assert client.post("/api/v1/auth/initialize", json={"username": "admin", "password": "another-password-123"}).status_code == 409
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "initial-password-123"})
    assert login.status_code == 200
    assert client.get("/api/v1/auth/me").json()["data"]["username"] == "admin"
    changed = client.post("/api/v1/auth/change-password", json={"current_password": "initial-password-123", "new_password": "replacement-password-123"})
    assert changed.status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "replacement-password-123"}).status_code == 200

