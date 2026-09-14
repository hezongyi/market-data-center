import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from data_center.deployment import (
    MANIFEST_HASH_FILE,
    DeploymentManifest,
    DeploymentService,
    _configured_data_hash,
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


def test_activation_failure_can_recover_pre_sidecar_current_release(tmp_path, monkeypatch):
    root = tmp_path / "releases"
    previous = release(root, "legacy-current")
    (previous / MANIFEST_HASH_FILE).unlink()
    release(root, "candidate")
    (root / "current").symlink_to(previous)
    service = DeploymentService(tmp_path, root, evidence_root=tmp_path / "evidence")
    calls = []

    def verify(identity):
        calls.append(identity["deployment_id"])
        if identity["deployment_id"] == "candidate":
            raise RuntimeError("injected readiness failure")

    monkeypatch.setattr(service, "_restart_and_verify", verify)
    with pytest.raises(RuntimeError, match="injected"):
        service.activate("candidate")
    assert (root / "current").resolve() == previous.resolve()
    assert calls == ["candidate", "legacy-current"]


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


def test_logical_ledger_hash_ignores_heartbeat_but_detects_run_changes(tmp_path, monkeypatch):
    from data_center.runs.ledger import RunLedger

    path = tmp_path / "ledger.sqlite"
    ledger = RunLedger(path)
    run_id = ledger.enqueue_job({"job_id": "x", "dataset_id": "provider_bars"})
    monkeypatch.setenv("DATACENTER_LEDGER_PATH", str(path))
    before = _configured_data_hash("DATACENTER_LEDGER_PATH")
    ledger.heartbeat()
    assert _configured_data_hash("DATACENTER_LEDGER_PATH") == before
    ledger.update(run_id, request_id="changed")
    assert _configured_data_hash("DATACENTER_LEDGER_PATH") != before


def test_logical_ledger_hash_covers_scheduler_tables_and_reports_cost(tmp_path, monkeypatch):
    """Every scheduling table is hashed, and the receipt can show what it cost."""
    from data_center.production_tasks import ProductionTasks
    from data_center.runs.ledger import RunLedger

    path = tmp_path / "ledger.sqlite"
    ledger = RunLedger(path)
    service = ProductionTasks(ledger)
    service.create(definition={"provider": "dukascopy", "symbol": "EURUSD", "bar_timeframes": [],
                               "window_policy": {"mode": "continuous",
                                                 "history_start": "2026-01-01T00:00:00+00:00"},
                               "schedule": {"schedule": "manual"}},
                   name="EURUSD", task_id="p1",
                   now=datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc))
    monkeypatch.setenv("DATACENTER_LEDGER_PATH", str(path))
    details: dict = {}
    before = _configured_data_hash("DATACENTER_LEDGER_PATH", details=details)
    # The scheduler state tables are inside the hash, not on an exclusion list.
    assert {"production_tasks", "plan_ownership", "production_task_versions",
            "scheduler_state", "scheduler_leases", "production_executions"} <= set(details["tables"])
    assert details["tables"]["production_tasks"] == {"rows": 1, "seconds": details["tables"]["production_tasks"]["seconds"]}
    assert details["row_count"] >= 1 and details["hash_seconds"] >= 0
    ledger.set_global_dispatch(False, actor="system:test")
    assert _configured_data_hash("DATACENTER_LEDGER_PATH") != before
    changed: dict = {}
    _configured_data_hash("DATACENTER_LEDGER_PATH", details=changed)
    assert changed["tables"]["scheduler_state"]["rows"] == 1


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
                 "market-data-center-monitor.service",
                 "market-data-center-scheduler.service",
                 "market-data-center-retention-audit.service"):
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


def fake_systemctl(tmp_path: Path, *, scheduler_state: str = "active",
                   failures_before_recovery: int = 0) -> Path:
    """A systemctl stand-in that logs every call and reports the unit states.

    ``failures_before_recovery`` reports a dead scheduler for that many checks and
    then recovers, so the rollback path (which verifies the previous release) can
    succeed while the candidate's own verification still fails.
    """
    script = tmp_path / "fake-systemctl"
    counter = tmp_path / "scheduler-failures"
    counter.write_text(str(failures_before_recovery))
    script.write_text(f"""#!/bin/sh
echo "$@" >> "{tmp_path / 'systemctl.log'}"
if [ "$1" = "is-active" ]; then
  if [ "$2" = "market-data-center-scheduler.service" ]; then
    remaining=$(cat "{counter}")
    if [ "$remaining" -gt 0 ]; then
      printf '%s' "$((remaining - 1))" > "{counter}"
      echo "{scheduler_state}"
      exit 3
    fi
  fi
  echo active
fi
exit 0
""")
    script.chmod(0o755)
    return script


def readiness_server(identity):
    """A minimal readiness endpoint; ``identity`` is called per request.

    A callable keeps the rollback drill honest: after a failed activation the
    pointer moves back, and readiness must report whichever release is current.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    resolved = identity if callable(identity) else (lambda: identity)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps({"data": resolved()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # keep the test output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/api/v1/health/ready"


def stop_readiness_server(server) -> None:
    """Stop and close the socket: a leaked one is a real test failure here."""
    server.shutdown()
    server.server_close()


def test_activation_verifies_every_unit_including_the_scheduler(tmp_path, monkeypatch):
    from data_center.deployment import runtime_identity

    root = tmp_path / "releases"
    release(root, "candidate")
    monkeypatch.setenv("DATACENTER_CANONICAL_ROOT", str(tmp_path / "canonical"))
    monkeypatch.setenv("DATACENTER_LEDGER_PATH", str(tmp_path / "ledger.sqlite"))
    identity = runtime_identity(root / "candidate" / "deployment.json")
    server, ready_url = readiness_server({
        "deployment_id": identity["deployment_id"], "software_version": identity["software_version"],
        "source_commit": identity["source_commit"]})
    service = DeploymentService(tmp_path, root, evidence_root=tmp_path / "evidence",
                                systemctl=(str(fake_systemctl(tmp_path)),), ready_url=ready_url)
    try:
        result = service.activate("candidate")
    finally:
        stop_readiness_server(server)

    calls = (tmp_path / "systemctl.log").read_text().splitlines()
    for unit in ("market-data-center-api.service", "market-data-center-worker.service",
                 "market-data-center-scheduler.service"):
        assert f"restart {unit}" in calls
        assert f"is-active {unit}" in calls
    # Readiness only proves the API answers; each unit's own state is recorded.
    assert result["services_active"] == [
        {"service": "market-data-center-api.service", "state": "active"},
        {"service": "market-data-center-worker.service", "state": "active"},
        {"service": "market-data-center-scheduler.service", "state": "active"},
    ]
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert receipt["details"]["services_active"] == result["services_active"]


def test_a_dead_scheduler_unit_fails_activation_and_rolls_back(tmp_path, monkeypatch):
    """A release is not activated while one of its components is already dead."""
    root = tmp_path / "releases"
    previous = release(root, "previous")
    (root / "current").symlink_to(previous)
    release(root, "candidate")
    monkeypatch.setenv("DATACENTER_CANONICAL_ROOT", str(tmp_path / "canonical"))
    monkeypatch.setenv("DATACENTER_LEDGER_PATH", str(tmp_path / "ledger.sqlite"))
    from data_center.deployment import runtime_identity

    server, ready_url = readiness_server(
        lambda: runtime_identity(root / "current" / "deployment.json"))
    # The candidate's scheduler unit is dead; the previous release's is healthy.
    script = fake_systemctl(tmp_path, scheduler_state="failed", failures_before_recovery=1)
    service = DeploymentService(tmp_path, root, evidence_root=tmp_path / "evidence",
                                systemctl=(str(script),), ready_url=ready_url)
    try:
        with pytest.raises(RuntimeError, match="scheduler.service is not active"):
            service.activate("candidate")
    finally:
        stop_readiness_server(server)
    assert service.current()["deployment_id"] == "previous"
    receipts = sorted((tmp_path / "evidence" / "operations" / "deployment_activate").glob("*.json"))
    payload = json.loads(receipts[0].read_text())
    assert payload["result"] == "failed"
    assert payload["failure_stage"] == "activate_or_verify"
    assert payload["details"]["recovered_deployment_id"] == "previous"
    # The candidate was rejected because of its own unit, not the platform's.
    errors = [line for line in (tmp_path / "systemctl.log").read_text().splitlines()
              if line.startswith("is-active")]
    assert errors.count("is-active market-data-center-scheduler.service") >= 2
