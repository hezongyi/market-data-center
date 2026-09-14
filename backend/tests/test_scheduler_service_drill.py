"""Service drill for the scheduler release: start, fail closed, restart, upgrade.

Plan S5.3 asks for an isolated start / failure fallback / restart drill of the
scheduler service and its immutable deployment before any production takeover.
Nothing here installs a unit or touches a production path: each drill runs the
real process against isolated canonical, ledger and evidence roots, and asserts
what the receipts and the ledger actually recorded.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from data_center.deployment import MANIFEST_HASH_FILE, DeploymentManifest, sha256_path
from data_center.runs.ledger import MIGRATIONS, SCHEMA_VERSION, RunLedger

REPOSITORY = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def release(root: Path, deployment_id: str) -> Path:
    """A minimal immutable release tree, exactly what identity validation reads."""
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
    (target / MANIFEST_HASH_FILE).write_text(
        hashlib.sha256((target / "deployment.json").read_bytes()).hexdigest() + "\n")
    return target


def environment(tmp_path, *, manifest: Path | None = None, instance_id: str | None = None,
                lease_seconds: float | None = None) -> dict:
    values = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(REPOSITORY / "backend" / "src"),
        "DATACENTER_CANONICAL_ROOT": str(tmp_path / "lake"),
        "DATACENTER_LEDGER_PATH": str(tmp_path / "ledger.sqlite"),
        "DATACENTER_EVIDENCE_ROOT": str(tmp_path / "evidence"),
    }
    if lease_seconds is not None:
        values["DATACENTER_SCHEDULER_LEASE_SECONDS"] = str(lease_seconds)
    if manifest is not None:
        values["DATACENTER_DEPLOYMENT_MANIFEST"] = str(manifest)
    if instance_id is not None:
        values["DATACENTER_SCHEDULER_INSTANCE_ID"] = instance_id
    return values


def start(tmp_path, *args: str, manifest: Path | None = None, instance_id: str | None = None,
          lease_seconds: float | None = None):
    return subprocess.Popen(
        [sys.executable, "-m", "data_center.scheduler_main", *args],
        cwd=REPOSITORY,
        env=environment(tmp_path, manifest=manifest, instance_id=instance_id,
                        lease_seconds=lease_seconds),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_isolated_start_reports_its_release_and_writes_identity_receipts(tmp_path):
    """The service starts against isolated roots and proves which release it is."""
    target = release(tmp_path / "releases", "drill-release")
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    ledger.create_production_task(
        task_id="p1", name="EURUSD", provider="dukascopy", symbol="EURUSD",
        payload={"schedule": {"schedule": "fixed_rate", "interval_seconds": 900,
                              "anchor": "2026-09-14T12:00:00+00:00"}},
        ownership_keys=["provider_bars:dukascopy:EURUSD:1m:bid"], desired_state="enabled",
        next_run_at="2026-09-14T11:59:00+00:00")

    process = start(tmp_path, "--once", manifest=target / "deployment.json", instance_id="drill-one")
    stdout, stderr = process.communicate(timeout=60)
    assert process.returncode == 0, stderr
    started = json.loads(stdout.splitlines()[0])
    # The process reports the immutable release identity it validated, not a guess.
    assert started["event"] == "scheduler_started"
    assert (started["deployment_id"], started["software_version"], started["source_commit"]) == (
        "drill-release", "1.0.0", "a" * 40)
    assert started["dispatch_enabled"] is False

    receipts = sorted((tmp_path / "evidence" / "operations" / "scheduler_tick").glob("*.json"))
    assert len(receipts) == 1, receipts
    receipt = json.loads(receipts[0].read_text())
    # The receipt proves which release produced it, at the top level and in the
    # identity the process validated.
    assert receipt["deployment_id"] == "drill-release"
    assert receipt["commit"] == "a" * 40 and receipt["software_version"] == "1.0.0"
    assert receipt["details"]["evaluated"] == 1
    # Shadow mode observed the due plan and created nothing: the drill must not
    # produce data as a side effect of starting.
    assert receipt["details"]["decisions"][0]["action"] == "shadow_start_execution"
    assert ledger.list_production_executions("p1") == []
    assert [row for row in ledger.list() if row.get("plan_id") == "p1"] == []


def test_a_release_that_cannot_prove_its_identity_fails_closed(tmp_path):
    """A tampered release must not plan work, and must say so with a receipt."""
    target = release(tmp_path / "releases", "tampered-release")
    (target / "application.py").write_text("VERSION = 2\n")  # artifact no longer matches

    process = start(tmp_path, "--once", manifest=target / "deployment.json", instance_id="drill-one")
    stdout, stderr = process.communicate(timeout=60)
    assert process.returncode == 3, (process.returncode, stdout, stderr)
    events = [json.loads(line) for line in stdout.splitlines()]
    assert [event["event"] for event in events] == ["scheduler_identity_failed"]
    assert events[0]["error_category"] == "RuntimeError"
    # The failure is receipted, and the ledger was never opened with that identity.
    failures = list((tmp_path / "evidence" / "operations" / "deployment_runtime_failure").glob("*.json"))
    assert len(failures) == 1, failures
    receipt = json.loads(failures[0].read_text())
    assert receipt["details"]["component"] == "scheduler"
    assert receipt["failure_stage"] == "runtime_identity"
    # Nothing was planned, claimed or published by a release that cannot identify
    # itself: the drill opens whatever file exists and finds it untouched.
    if (tmp_path / "ledger.sqlite").exists():
        untouched = RunLedger(tmp_path / "ledger.sqlite")
        assert untouched.list_production_tasks() == [] and untouched.list() == []
        assert untouched.scheduler_state()["tick_count"] == 0


def test_restart_takes_the_lease_over_after_a_crash(tmp_path):
    """A killed instance leaves the lease to expire; the next one takes it over."""
    ledger_path = tmp_path / "ledger.sqlite"
    # A one-second lease keeps the drill honest about takeover without waiting for
    # the production TTL; the mechanism under test is unchanged.
    first = start(tmp_path, "--interval-seconds", "30", instance_id="drill-one",
                  lease_seconds=1.0)
    try:
        started = json.loads(first.stdout.readline())
        assert started["event"] == "scheduler_started"
        json.loads(first.stdout.readline())
        held = RunLedger(ledger_path).scheduler_state()["lease"]
        assert held["owner_id"] == "drill-one" and held["fencing_token"] == 1
        # A crash, not a clean stop: the lease row survives with its expiry.
        first.kill()
        first.communicate(timeout=30)
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=10)

    # Wait out the (drill-shortened) lease the killed process still held: a
    # takeover is defined by the expiry, so this waits for it instead of racing it.
    time.sleep(1.5)
    # The restarted process reclaims the lease and its fencing token advances, so
    # a stale writer from the previous instance can be rejected.
    second = start(tmp_path, "--once", instance_id="drill-two", lease_seconds=1.0)
    stdout, stderr = second.communicate(timeout=60)
    assert second.returncode == 0, stderr
    assert json.loads(stdout.splitlines()[0])["event"] == "scheduler_started"
    ledger = RunLedger(ledger_path)
    state = ledger.scheduler_state()
    assert state["instance_id"] == "drill-two"
    assert state["lease"]["owner_id"] == "drill-two"
    assert state["lease"]["fencing_token"] == 2
    # Both instances ticked once: the counter is the platform's, not the lease's.
    assert state["tick_count"] == 2


def test_an_older_schema_upgrades_in_place_before_the_service_plans(tmp_path):
    """The incremental schema must take an existing database forward, not reset it."""
    ledger_path = tmp_path / "ledger.sqlite"
    connection = sqlite3.connect(ledger_path)
    try:
        for version in sorted(MIGRATIONS):
            if version >= SCHEMA_VERSION:
                break
            MIGRATIONS[version](connection)
            connection.execute(f"pragma user_version={int(version)}")
        connection.execute(
            "insert into runs(run_id,payload,status,created_at) values ('legacy-run',?, 'pass', ?)",
            (json.dumps({"run_id": "legacy-run", "status": "pass", "provider": "dukascopy"}),
             NOW.isoformat()))
        connection.execute(
            "insert into production_tasks(task_id,name,payload,desired_state,definition_version,"
            "created_at,updated_at,provider,symbol) "
            "values ('legacy-plan','legacy','{}','paused',1,?,?,'dukascopy','EURUSD')",
            (NOW.isoformat(), NOW.isoformat()))
        connection.commit()
        assert connection.execute("pragma user_version").fetchone()[0] == SCHEMA_VERSION - 1
    finally:
        connection.close()

    ledger = RunLedger(ledger_path)
    assert ledger.schema_version() == SCHEMA_VERSION
    # Existing records survive the upgrade, and the new table is usable.
    assert ledger.get("legacy-run")["status"] == "pass"
    assert [run["run_id"] for run in ledger.list()] == ["legacy-run"]
    assert ledger.get_production_task("legacy-plan")["task_id"] == "legacy-plan"
    ledger.record_provider_backoff("dukascopy", until=NOW, failures=1, reason="drill")
    assert [row["provider"] for row in ledger.provider_backoff(
        now=NOW - timedelta(minutes=1))] == ["dukascopy"]
