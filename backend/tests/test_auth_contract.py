from fastapi.testclient import TestClient

from data_center.api.app import create_app
from data_center.runs.ledger import RunLedger
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


def test_session_actor_and_password_state_survive_app_restart(tmp_path):
    state = tmp_path / "auth-state.json"
    settings = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "ledger.sqlite",
                        evidence_root=tmp_path / "evidence", auth_cookie_secure=False, auth_state_path=state)
    first = TestClient(create_app(settings))
    assert first.post("/api/v1/auth/initialize", json={"username": "admin", "password": "initial-password-123"}).status_code == 200
    assert state.exists()
    assert first.post("/api/v1/auth/login", json={"username": "admin", "password": "initial-password-123"}).status_code == 200
    payload = {"run_kind": "ingest", "run_scope": "acceptance", "provider": "fixture", "symbol": "UI_TEST",
               "asset_class": "test", "timeframe": "1d", "start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z"}
    response = first.post("/api/v1/maintenance/tasks", json=payload)
    assert response.status_code in (200, 202)
    assert RunLedger(settings.ledger_path).write_audit_entries()[0]["actor"] == "session:admin"
    restarted = TestClient(create_app(Settings(canonical_root=settings.canonical_root, ledger_path=settings.ledger_path,
                                               evidence_root=settings.evidence_root, auth_cookie_secure=False,
                                               auth_state_path=state)))
    assert restarted.post("/api/v1/auth/login", json={"username": "admin", "password": "initial-password-123"}).status_code == 200
