from datetime import datetime, timezone

from fastapi.testclient import TestClient

from data_center.api.app import create_app
from data_center.settings import Settings


def test_runs_and_quality_contract(tmp_path) -> None:
    app = create_app(Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                              evidence_root=tmp_path / "evidence"))
    client = TestClient(app)
    job = {"job_id": "test-1", "symbol": "BTCUSDT", "start": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(), "end": datetime(2026, 1, 2, tzinfo=timezone.utc).isoformat()}
    response = client.post("/api/v1/quality/checks", json=job)
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "pass"


def test_write_api_requires_key(tmp_path) -> None:
    app = create_app(Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                              evidence_root=tmp_path / "evidence", api_key="secret"))
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
    app = create_app(Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                              evidence_root=tmp_path / "evidence", webui_dist=dist))
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "data center" in response.text


def test_validation_errors_use_api_envelope(tmp_path) -> None:
    app = create_app(Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                              evidence_root=tmp_path / "evidence"))
    response = TestClient(app).post("/api/v1/ingest/runs", json={"job_id": "invalid"})
    assert response.status_code == 422
    assert response.json()["data"] is None
    assert response.json()["errors"][0]["code"] == "validation_error"


def test_retry_authorization_and_request_correlation(tmp_path):
    from data_center.runs.ledger import RunLedger

    config = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                      evidence_root=tmp_path / "evidence", api_key="secret")
    ledger = RunLedger(config.ledger_path)
    client = TestClient(create_app(config))
    job = {"job_id": "correlated", "symbol": "TEST", "start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z"}
    response = client.post("/api/v1/ingest/runs", json=job, headers={"X-API-Key": "secret", "X-Request-ID": "trace-1"})
    run_id = response.json()["data"]["run_id"]
    assert response.headers["X-Request-ID"] == response.json()["meta"]["request_id"] == "trace-1"
    assert ledger.get(run_id)["request_id"] == "trace-1"
    claimed = ledger.claim_next_job()
    ledger.fail_job(claimed["job_id"], run_id, "invalid", retryable=False)
    original = ledger.get(run_id)
    denied = client.post(f"/api/v1/runs/{run_id}/retry")
    assert denied.status_code == 401
    assert denied.headers["X-Request-ID"] == denied.json()["meta"]["request_id"]
    assert len(ledger.list()) == 1
    retried = client.post(f"/api/v1/runs/{run_id}/retry", headers={"X-API-Key": "secret"})
    assert retried.status_code == 202
    assert retried.json()["data"]["retry_of"] == run_id
    assert ledger.get(run_id) == original
    assert len(client.get("/api/v1/runs?status=failed").json()["data"]) == 1


def test_published_manifest_endpoint_is_read_only_and_validated(tmp_path):
    from data_center.catalog.manifest import build_manifest, write_manifest
    from data_center.domain.models import ProviderBar
    from data_center.storage.parquet import write_provider_bars

    root = tmp_path / "lake"
    rows = [ProviderBar(symbol="TEST", asset_class="crypto", provider="fixture", timeframe="1d",
                        bar_ts=datetime(2026, 1, 1, tzinfo=timezone.utc), open=1, high=2, low=0, close=1,
                        volume=1, ingest_ts=datetime(2026, 1, 2, tzinfo=timezone.utc), source_hash="x")]
    path = write_provider_bars(root, rows, part_id="manifest-api")
    write_manifest(root, build_manifest(root, run_id="manifest-api", dataset_id="provider_bars",
                                        schema_version="provider_bars.v1", paths=path, row_count=1,
                                        quality_summary={"status": "pass", "finding_count": 0, "findings": []}))
    # Endpoint only exposes a manifest after the publication boundary has been recorded.
    app = create_app(Settings(canonical_root=root, ledger_path=tmp_path / "runs.sqlite",
                              evidence_root=tmp_path / "evidence"))
    response = TestClient(app).get("/api/v1/runs/manifest-api/manifest")
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["status"] == "published" and payload["parts"][0]["bytes"] == path[0].stat().st_size
    assert TestClient(app).get("/api/v1/runs/missing/manifest").status_code == 404
