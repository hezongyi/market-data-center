import hashlib
import json
from pathlib import Path

import pytest

from data_center.deployment import (
    MANIFEST_HASH_FILE,
    DeploymentManifest,
    DeploymentService,
    runtime_identity,
    sha256_path,
    validated_runtime_identity,
)


def release(root: Path, deployment_id: str) -> Path:
    target = root / deployment_id
    target.mkdir(parents=True)
    (target / "application.py").write_text("VERSION = 1\n")
    manifest = DeploymentManifest(
        deployment_id=deployment_id, software_version="1.0.0", source_commit="a" * 40,
        tag="v1.0.0", artifact_sha256=sha256_path(target), python_version="3.11.0",
        constraints_sha256="b" * 64, web_ui_asset_sha256="c" * 64,
        created_at="2026-09-10T00:00:00+00:00", activated_at=None,
        previous_deployment_id=None,
    )
    (target / "deployment.json").write_text(json.dumps(manifest.as_dict()))
    (target / MANIFEST_HASH_FILE).write_text(hashlib.sha256((target / "deployment.json").read_bytes()).hexdigest() + "\n")
    return target


def test_runtime_identity_fails_closed_on_missing_or_tampered_manifest(tmp_path):
    with pytest.raises(RuntimeError, match="missing"):
        runtime_identity(tmp_path / "missing.json")
    target = release(tmp_path, "release-a")
    assert runtime_identity(target / "deployment.json")["deployment_id"] == "release-a"
    (target / "application.py").write_text("VERSION = 2\n")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        runtime_identity(target / "deployment.json")


def test_runtime_identity_rejects_manifest_tampering(tmp_path):
    target = release(tmp_path, "release-a")
    payload = json.loads((target / "deployment.json").read_text())
    payload["source_commit"] = "f" * 40
    (target / "deployment.json").write_text(json.dumps(payload))
    with pytest.raises(RuntimeError, match="manifest hash mismatch"):
        runtime_identity(target / "deployment.json")


def test_runtime_identity_failure_writes_safe_component_receipt(tmp_path):
    evidence = tmp_path / "evidence"
    with pytest.raises(RuntimeError, match="missing"):
        validated_runtime_identity(
            tmp_path / "missing" / "deployment.json", evidence, component="api",
        )
    receipt_path = next((evidence / "operations" / "deployment_runtime_failure").glob("*.json"))
    receipt = json.loads(receipt_path.read_text())
    assert receipt["result"] == "failed"
    assert receipt["details"] == {"component": "api"}
    assert str(tmp_path) not in json.dumps(receipt)


def test_operation_receipt_uses_immutable_deployment_identity(tmp_path, monkeypatch):
    from data_center.evidence import operation_receipt

    target = release(tmp_path, "receipt-release")
    monkeypatch.setenv("DATACENTER_DEPLOYMENT_MANIFEST", str(target / "deployment.json"))
    receipt = operation_receipt(
        action="monitor", command="test", started_at="2026-09-10T00:00:00+00:00", result="pass",
    )
    assert receipt["deployment_id"] == "receipt-release"
    assert receipt["software_version"] == "1.0.0"
    assert receipt["commit"] == "a" * 40


def test_activation_failure_restores_previous_release_and_records_receipt(tmp_path, monkeypatch):
    root = tmp_path / "releases"
    previous = release(root, "previous")
    release(root, "candidate")
    (root / "current").symlink_to(previous)
    service = DeploymentService(tmp_path, root, evidence_root=tmp_path / "evidence")
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    canonical_manifest = canonical / "manifest.json"
    canonical_manifest.write_text('{"stable": true}\n')
    ledger = tmp_path / "ledger.sqlite"
    ledger.write_bytes(b"stable-ledger")
    monkeypatch.setenv("DATACENTER_CANONICAL_ROOT", str(canonical))
    monkeypatch.setenv("DATACENTER_LEDGER_PATH", str(ledger))
    canonical_before = canonical_manifest.read_bytes()
    ledger_before = ledger.read_bytes()
    calls = []

    def verify(identity):
        calls.append(identity["deployment_id"])
        if identity["deployment_id"] == "candidate":
            raise RuntimeError("injected readiness failure")

    monkeypatch.setattr(service, "_restart_and_verify", verify)
    with pytest.raises(RuntimeError, match="injected"):
        service.activate("candidate")
    assert service.current()["deployment_id"] == "previous"
    assert calls == ["candidate", "previous"]
    receipts = list((tmp_path / "evidence" / "operations" / "deployment_activate").glob("*.json"))
    payload = json.loads(receipts[0].read_text())
    assert payload["result"] == "failed"
    assert payload["details"]["recovered_deployment_id"] == "previous"
    assert payload["details"]["canonical_hash_unchanged"] is True
    assert payload["details"]["ledger_hash_unchanged"] is True
    assert canonical_manifest.read_bytes() == canonical_before
    assert ledger.read_bytes() == ledger_before


def test_initial_activation_failure_removes_current_pointer(tmp_path, monkeypatch):
    root = tmp_path / "releases"
    release(root, "candidate")
    service = DeploymentService(tmp_path, root, evidence_root=tmp_path / "evidence")
    monkeypatch.setattr(
        service, "_restart_and_verify",
        lambda identity: (_ for _ in ()).throw(RuntimeError("injected readiness failure")),
    )

    with pytest.raises(RuntimeError, match="injected"):
        service.activate("candidate")

    assert not (root / "current").exists()
    payload = json.loads(next((tmp_path / "evidence" / "operations" / "deployment_activate").glob("*.json")).read_text())
    assert payload["details"]["recovered_deployment_id"] is None


def test_activate_rejects_release_path_escape_and_symlink(tmp_path):
    root = tmp_path / "releases"
    outside = release(tmp_path, "outside")
    root.mkdir()
    (root / "linked").symlink_to(outside)
    service = DeploymentService(tmp_path, root)

    for release_id in ("../outside", "linked", "current"):
        with pytest.raises(ValueError, match="release"):
            service.activate(release_id)


def test_successful_activation_preserves_canonical_and_ledger(tmp_path, monkeypatch):
    root = tmp_path / "releases"
    release(root, "candidate")
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    manifest = canonical / "manifest.json"
    manifest.write_text('{"stable": true}\n')
    ledger = tmp_path / "ledger.sqlite"
    ledger.write_bytes(b"stable-ledger")
    monkeypatch.setenv("DATACENTER_CANONICAL_ROOT", str(canonical))
    monkeypatch.setenv("DATACENTER_LEDGER_PATH", str(ledger))
    service = DeploymentService(tmp_path, root, evidence_root=tmp_path / "evidence")
    monkeypatch.setattr(service, "_restart_and_verify", lambda identity: None)

    result = service.activate("candidate")

    assert result["canonical_hash_unchanged"] is True
    assert result["ledger_hash_unchanged"] is True
    assert manifest.read_text() == '{"stable": true}\n'
    assert ledger.read_bytes() == b"stable-ledger"


def test_dirty_checkout_is_rejected_before_build(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "dirty.txt").write_text("dirty")
    service = DeploymentService(tmp_path, tmp_path / "releases")
    with pytest.raises(ValueError, match="dirty checkout"):
        service.stage()


def test_stage_rejects_commit_not_reachable_from_protected_main(tmp_path, monkeypatch):
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "tracked.txt").write_text("main\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "main"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "branch", "origin/main"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "switch", "-qc", "feature"], check=True)
    (tmp_path / "tracked.txt").write_text("feature\n")
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qam", "feature"], check=True)
    service = DeploymentService(tmp_path, tmp_path / "releases")
    monkeypatch.setattr(service, "_build_runtime", lambda target: None)

    with pytest.raises(ValueError, match="protected ref"):
        service.stage("HEAD")


def test_release_source_rejects_credentials_and_development_artifacts(tmp_path):
    service = DeploymentService(tmp_path, tmp_path / "releases")
    source = tmp_path / "candidate"
    source.mkdir()
    (source / ".env.local").write_text("SECRET=value\n")
    with pytest.raises(ValueError, match="forbidden paths"):
        service._validate_release_tree(source)


def test_api_worker_monitor_report_same_deployment_identity(tmp_path, monkeypatch, capsys):
    from fastapi.testclient import TestClient

    from data_center.api.app import create_app
    from data_center.observability import main as monitor_main
    from data_center.runs.ledger import RunLedger
    from data_center.settings import Settings
    from data_center.worker_main import main as worker_main

    target = release(tmp_path / "releases", "identity-release")
    canonical = tmp_path / "canonical"
    ledger_path = canonical / "audit" / "ledger.sqlite"
    evidence = tmp_path / "evidence"
    ledger = RunLedger(ledger_path)
    ledger.heartbeat()
    values = {
        "DATACENTER_CANONICAL_ROOT": canonical,
        "DATACENTER_LEDGER_PATH": ledger_path,
        "DATACENTER_EVIDENCE_ROOT": evidence,
        "DATACENTER_DEPLOYMENT_MANIFEST": target / "deployment.json",
        "DATACENTER_BACKUP_ROOT": tmp_path / "backups",
        "DATACENTER_RESTORE_STAGING_ROOT": tmp_path / "restore-staging",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, str(value))
    identity = {"deployment_id": "identity-release", "software_version": "1.0.0", "source_commit": "a" * 40}
    client = TestClient(create_app(Settings()))
    ready = client.get("/api/v1/health/ready").json()["data"]
    metrics = client.get("/api/v1/metrics").json()["data"]
    assert {key: ready[key] for key in identity} == identity
    assert {key: metrics[key] for key in identity} == identity

    class StopWorker:
        def __init__(self, *args, **kwargs):
            pass

        def run_next(self):
            raise SystemExit(0)

    monkeypatch.setattr("data_center.worker_main.LocalWorker", StopWorker)
    with pytest.raises(SystemExit):
        worker_main()
    worker_log = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert {key: worker_log[key] for key in identity} == identity

    monitor_main()
    monitor_log = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert {key: monitor_log[key] for key in identity} == identity
    receipt_path = Path(monitor_log["receipt"])
    receipt = json.loads(receipt_path.read_text())
    assert receipt["details"]["deployment_id"] == identity["deployment_id"]


def test_systemd_units_use_immutable_release_and_non_overlapping_monitor_timer():
    repository = Path(__file__).resolve().parents[2]
    unit_root = repository / "deploy" / "systemd"
    for name in ("market-data-center-api.service", "market-data-center-worker.service",
                 "market-data-center-monitor.service"):
        unit = (unit_root / name).read_text()
        assert "WorkingDirectory=%h/market-data-center/releases/current" in unit
        assert "DATACENTER_DEPLOYMENT_MANIFEST=%h/market-data-center/releases/current/deployment.json" in unit
        assert "ExecStart=%h/market-data-center/releases/current/" in unit
        assert "%h/.config/market-data-center/env" in unit
    monitor = (unit_root / "market-data-center-monitor.service").read_text()
    timer = (unit_root / "market-data-center-monitor.timer").read_text()
    assert "RuntimeMaxSec=45s" in monitor
    assert "OnUnitInactiveSec=60s" in timer
    assert "OnUnitActiveSec" not in timer
