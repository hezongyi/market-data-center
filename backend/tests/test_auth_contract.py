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
def test_auth_password_rotation_is_visible_to_second_app(tmp_path):
    state = tmp_path / "auth-state.json"
    def config():
        return Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "ledger.sqlite",
                        evidence_root=tmp_path / "evidence", auth_state_path=state, auth_cookie_secure=False)
    first = TestClient(create_app(config()))
    assert first.post("/api/v1/auth/initialize", json={"username": "admin", "password": "initial-password-123"}).status_code == 200
    assert first.post("/api/v1/auth/login", json={"username": "admin", "password": "initial-password-123"}).status_code == 200
    second = TestClient(create_app(config()))
    assert first.post("/api/v1/auth/change-password", json={"current_password": "initial-password-123", "new_password": "replacement-password-123"}).status_code == 200
    assert second.post("/api/v1/auth/login", json={"username": "admin", "password": "initial-password-123"}).status_code == 401
    assert second.post("/api/v1/auth/login", json={"username": "admin", "password": "replacement-password-123"}).status_code == 200
def test_session_actor_and_password_state_survive_app_restart(tmp_path):
    state = tmp_path / "auth-state.json"
    settings = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "ledger.sqlite",
                        evidence_root=tmp_path / "evidence", auth_cookie_secure=False, auth_state_path=state)
    first = TestClient(create_app(settings))
    assert first.post("/api/v1/auth/initialize", json={"username": "admin", "password": "initial-password-123"}).status_code == 200
    assert settings.ledger_path.with_name("auth.sqlite3").exists()
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

def test_concurrent_worker_logins_preserve_both_sessions(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    state = tmp_path / "auth-state.json"
    def config():
        return Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "ledger.sqlite",
                        evidence_root=tmp_path / "evidence", auth_state_path=state, auth_cookie_secure=False)
    first = TestClient(create_app(config()))
    assert first.post("/api/v1/auth/initialize", json={"username": "admin", "password": "initial-password-123"}).status_code == 200
    def login():
        client = TestClient(create_app(config()))
        return client, client.post("/api/v1/auth/login", json={"username": "admin", "password": "initial-password-123"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result, second_result = list(pool.map(lambda _: login(), range(2)))
    a, login_a = first_result
    b, login_b = second_result
    assert login_a.status_code == 200
    assert login_b.status_code == 200
    assert a.get("/api/v1/auth/me").status_code == 200
    assert b.get("/api/v1/auth/me").status_code == 200

def test_concurrent_logout_preserves_other_worker_session(tmp_path):
    state = tmp_path / "auth-state.json"
    def config():
        return Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "ledger.sqlite",
                        evidence_root=tmp_path / "evidence", auth_state_path=state, auth_cookie_secure=False)
    bootstrap = TestClient(create_app(config()))
    bootstrap.post("/api/v1/auth/initialize", json={"username": "admin", "password": "initial-password-123"})
    a = TestClient(create_app(config())); b = TestClient(create_app(config()))
    a.post("/api/v1/auth/login", json={"username": "admin", "password": "initial-password-123"})
    b.post("/api/v1/auth/login", json={"username": "admin", "password": "initial-password-123"})
    a.post("/api/v1/auth/logout")
    assert a.get("/api/v1/auth/me").status_code == 401
    assert b.get("/api/v1/auth/me").status_code == 200

def test_password_change_revokes_sessions_across_workers(tmp_path):
    state = tmp_path / "auth-state.json"
    def config():
        return Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "ledger.sqlite",
                        evidence_root=tmp_path / "evidence", auth_state_path=state, auth_cookie_secure=False)
    bootstrap = TestClient(create_app(config()))
    bootstrap.post("/api/v1/auth/initialize", json={"username": "admin", "password": "initial-password-123"})
    a = TestClient(create_app(config())); b = TestClient(create_app(config()))
    a.post("/api/v1/auth/login", json={"username": "admin", "password": "initial-password-123"})
    b.post("/api/v1/auth/login", json={"username": "admin", "password": "initial-password-123"})
    assert a.post("/api/v1/auth/change-password", json={"current_password": "initial-password-123", "new_password": "replacement-password-123"}).status_code == 200
    assert b.get("/api/v1/auth/me").status_code == 401
    assert b.post("/api/v1/auth/login", json={"username": "admin", "password": "initial-password-123"}).status_code == 401
    assert b.post("/api/v1/auth/login", json={"username": "admin", "password": "replacement-password-123"}).status_code == 200


def test_preview_cookie_name_is_configurable_without_changing_the_default(tmp_path):
    settings = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "ledger.sqlite",
                        evidence_root=tmp_path / "evidence", auth_cookie_secure=False,
                        auth_cookie_name="mdc_preview_alpha")
    client = TestClient(create_app(settings))
    client.post("/api/v1/auth/initialize", json={"username": "admin", "password": "initial-password-123"})
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "initial-password-123"})
    assert "mdc_preview_alpha=" in login.headers["set-cookie"]
    assert "mdc_session=" not in login.headers["set-cookie"]
    assert client.get("/api/v1/auth/me").status_code == 200
    logout = client.post("/api/v1/auth/logout")
    assert "mdc_preview_alpha=" in logout.headers["set-cookie"]
    assert client.get("/api/v1/auth/me").status_code == 401
    assert Settings().auth_cookie_name == "mdc_session"
