import gzip
import os
import tarfile
import time
import tracemalloc

import pytest

from data_center.acceptance import (
    _rows_hash,
    archive_receipts,
    evidence_context,
    run_acceptance,
)
from data_center.operations import (
    BACKUP_FORMAT_V2,
    create_backup,
    daily_chunks,
    recovery_drill,
    restore_backup,
    retention_audit,
    verify_backup,
)


def publish_test_part(root, part):
    import hashlib
    import json

    manifest = root / ".manifests" / "test-run.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        "run_id": "test-run", "status": "published",
        "parts": [{"path": str(part.relative_to(root)), "bytes": part.stat().st_size,
                   "sha256": hashlib.sha256(part.read_bytes()).hexdigest()}],
    }))


def test_old_receipts_are_losslessly_archived(tmp_path):
    old = tmp_path / 'receipt-old.json'
    old.write_text('{"status":"failed"}')
    data = old.read_bytes()
    os.utime(old, (time.time() - 91 * 86400,) * 2)
    fresh = tmp_path / 'receipt-new.json'
    fresh.write_text('{}')
    other = tmp_path / 'ledger.sqlite'
    other.write_text('do not touch')
    assert archive_receipts(tmp_path) == ['receipt-old.json.gz']
    assert gzip.decompress((tmp_path / 'archive/receipt-old.json.gz').read_bytes()) == data
    assert not old.exists()
    assert fresh.exists() and other.exists()


def test_acceptance_minimum_interval_skips_network(tmp_path):
    (tmp_path / 'last-attempt').write_text(str(time.time()))
    assert run_acceptance('http://127.0.0.1:1', tmp_path)['status'] == 'skipped'


def test_acceptance_evidence_uses_immutable_deployment_identity(tmp_path, monkeypatch):
    import json

    from data_center.deployment import MANIFEST_HASH_FILE, sha256_path

    manifest = tmp_path / "deployment.json"
    manifest.write_text(json.dumps({
        "deployment_id": "release-1", "software_version": "0.2.0", "source_commit": "a" * 40,
        "tag": None, "artifact_sha256": sha256_path(tmp_path), "python_version": "3.11.15",
        "constraints_sha256": None, "web_ui_asset_sha256": None, "created_at": "2026-09-11T00:00:00Z",
        "activated_at": None, "previous_deployment_id": None,
        "release_format_version": "deployment-manifest.v1",
    }))
    (tmp_path / MANIFEST_HASH_FILE).write_text(sha256_path(manifest))
    monkeypatch.setenv("DATACENTER_DEPLOYMENT_MANIFEST", str(manifest))
    context = evidence_context()
    assert context["deployment_id"] == "release-1"
    assert context["software_version"] == "0.2.0"
    assert context["source_commit"] == "a" * 40


def test_acceptance_readback_hash_is_stable_for_key_order():
    assert _rows_hash([{"a": 1, "b": 2}]) == _rows_hash([{"b": 2, "a": 1}])


def test_acceptance_unreachable_service_persists_alert(tmp_path):
    import json
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        report = run_acceptance(f"http://127.0.0.1:{sock.getsockname()[1]}", tmp_path, spacing_seconds=0)
    assert report["status"] == "failed"
    alert = json.loads((tmp_path / "alerts.jsonl").read_text())
    assert set(alert["providers"]) == {"binance", "yfinance", "dukascopy", "fred"}
    assert json.loads((tmp_path / alert["receipt"]).read_text())["status"] == "failed"


def test_retention_audit_does_not_change_data(tmp_path):
    part = tmp_path / "part.parquet"
    part.write_bytes(b"unchanged")
    publish_test_part(tmp_path, part)
    os.utime(part, (time.time() - 40 * 86400,) * 2)
    report = retention_audit(tmp_path)
    assert report["canonical"]["old_files"] == 1
    assert report["capacity"]["total_bytes"] >= report["capacity"]["used_bytes"]
    assert part.read_bytes() == b"unchanged"


def test_daily_backfill_chunks_are_bounded_and_contiguous():
    from datetime import date
    from itertools import pairwise

    import pytest

    chunks = list(daily_chunks(date(2024, 1, 1), date(2026, 1, 1)))
    assert chunks[0][0] == date(2024, 1, 1) and chunks[-1][1] == date(2026, 1, 1)
    assert all(0 < (end - start).days <= 365 for start, end in chunks)
    assert all(left[1] == right[0] for left, right in pairwise(chunks))
    with pytest.raises(ValueError):
        list(daily_chunks(date(2026, 1, 1), date(2024, 1, 1)))


def test_completed_backfill_receipt_is_idempotent_without_network(tmp_path):
    import json
    from datetime import date

    from data_center.operations import backfill

    output = tmp_path / "backfill.json"
    output.write_text(json.dumps({"status": "pass", "provider": "fixture", "symbol": "TEST", "runs": []}))
    assert backfill("http://127.0.0.1:1", "fixture", "TEST", "crypto",
                     date(2026, 1, 1), date(2026, 1, 2), output)["status"] == "pass"


def test_backfill_resume_rejects_different_request(tmp_path):
    import json
    from datetime import date

    import pytest

    from data_center.operations import backfill

    output = tmp_path / "backfill.json"
    output.write_text(json.dumps({"status": "failed", "provider": "fixture", "symbol": "OTHER", "runs": []}))
    with pytest.raises(ValueError, match="different request"):
        backfill("http://127.0.0.1:1", "fixture", "TEST", "crypto",
                 date(2026, 1, 1), date(2026, 1, 2), output)


def test_backup_restore_is_verified_and_never_overwrites_conflicts(tmp_path):
    root = tmp_path / "canonical"
    ledger = root / "audit" / "ledger.sqlite"
    part = root / "provider_bars" / "part.parquet"
    part.parent.mkdir(parents=True)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"canonical-bytes")
    publish_test_part(root, part)
    ledger.write_bytes(b"ledger-bytes")
    (root / ".ingest-staging" / "running" / "part.parquet").parent.mkdir(parents=True)
    (root / ".ingest-staging" / "running" / "part.parquet").write_bytes(b"live")
    archive = tmp_path / "backup.tar.gz"
    report = create_backup(root, ledger, archive)
    assert report["status"] == "pass" and report["file_count"] == 3
    assert report["format"] == BACKUP_FORMAT_V2
    assert report["source_commit"]
    assert verify_backup(archive)["status"] == "pass"
    restored = tmp_path / "restored"
    restored_ledger = restored / "audit" / "data_center.sqlite"
    assert restore_backup(archive, restored, restored_ledger)["restored"] == 3
    assert (restored / "provider_bars/part.parquet").read_bytes() == b"canonical-bytes"
    assert restored_ledger.read_bytes() == b"ledger-bytes"
    with pytest.raises(ValueError, match="differs"):
        (restored / "provider_bars/part.parquet").write_bytes(b"changed")
        restore_backup(archive, restored, restored_ledger)
    drill = recovery_drill(root, ledger, tmp_path / "drill")
    assert drill["status"] == "pass" and drill["file_count"] == 3


def test_backup_rejects_ledger_outside_canonical_root(tmp_path):
    with pytest.raises(ValueError, match="inside"):
        create_backup(tmp_path / "canonical", tmp_path / "outside.sqlite", tmp_path / "backup.tar.gz")


def test_recovery_drill_compares_archive_snapshot_when_live_ledger_changes(tmp_path, monkeypatch):
    from data_center import operations

    root = tmp_path / "canonical"
    ledger = root / "audit" / "ledger.sqlite"
    ledger.parent.mkdir(parents=True)
    ledger.write_bytes(b"ledger-before-backup")
    original_create_backup = operations.create_backup

    def create_then_mutate(*args, **kwargs):
        report = original_create_backup(*args, **kwargs)
        ledger.write_bytes(b"ledger-after-backup")
        return report

    monkeypatch.setattr(operations, "create_backup", create_then_mutate)
    drill = operations.recovery_drill(root, ledger, tmp_path / "drill")
    assert drill["status"] == "pass"
    assert (tmp_path / "drill/restored-canonical/audit/data_center.sqlite").read_bytes() == b"ledger-before-backup"
    assert ledger.read_bytes() == b"ledger-after-backup"


def test_backup_uses_consistent_sqlite_snapshot_during_heartbeat(tmp_path):
    from data_center.runs.ledger import RunLedger

    root = tmp_path / "canonical"
    ledger_path = root / "audit" / "ledger.sqlite"
    ledger = RunLedger(ledger_path)
    ledger.enqueue_job({"job_id": "snapshot", "dataset_id": "provider_bars"})
    stop = False

    def heartbeat_loop():
        while not stop:
            ledger.heartbeat()

    import threading
    thread = threading.Thread(target=heartbeat_loop)
    thread.start()
    try:
        report = create_backup(root, ledger_path, tmp_path / "backup.tar.gz")
        assert verify_backup(tmp_path / "backup.tar.gz")["status"] == "pass"
        assert report["status"] == "pass"
    finally:
        stop = True
        thread.join(timeout=2)


def test_backup_failure_keeps_only_identifiable_temporary_artifact(tmp_path, monkeypatch):
    root = tmp_path / "canonical"
    ledger = root / "audit" / "ledger.sqlite"
    ledger.parent.mkdir(parents=True)
    ledger.write_bytes(b"ledger")
    destination = tmp_path / "backup.tar.gz"
    monkeypatch.setattr("data_center.operations._verify_backup",
                        lambda path: (_ for _ in ()).throw(ValueError("stop")))
    with pytest.raises(ValueError, match="stop"):
        create_backup(root, ledger, destination)
    assert not destination.exists()
    partials = list(tmp_path.glob(".*.partial"))
    assert len(partials) == 1
    with pytest.raises(ValueError, match="temporary"):
        restore_backup(partials[0], tmp_path / "restore", tmp_path / "restore/audit/ledger.sqlite")


def test_backup_directory_sync_failure_reverts_final_to_partial(tmp_path, monkeypatch):
    root = tmp_path / "canonical"
    ledger = root / "audit" / "ledger.sqlite"
    ledger.parent.mkdir(parents=True)
    ledger.write_bytes(b"ledger")
    destination = tmp_path / "backup.tar.gz"
    monkeypatch.setattr("data_center.operations._fsync_directory",
                        lambda path: (_ for _ in ()).throw(OSError("sync failed")))
    with pytest.raises(OSError, match="sync failed"):
        create_backup(root, ledger, destination)
    assert not destination.exists()
    assert len(list(tmp_path.glob(".*.partial"))) == 1


def test_restore_v1_compatibility_and_streaming_memory(tmp_path):
    root = tmp_path / "source"
    ledger = root / "audit" / "ledger.sqlite"
    part = root / "provider_bars" / "large.parquet"
    ledger.parent.mkdir(parents=True)
    part.parent.mkdir(parents=True)
    ledger.write_bytes(b"ledger")
    part.write_bytes(os.urandom(12 * 1024 * 1024))
    entries = [
        {"path": "canonical/provider_bars/large.parquet", "bytes": part.stat().st_size,
         "sha256": __import__("hashlib").sha256(part.read_bytes()).hexdigest()},
        {"path": "ledger.sqlite", "bytes": ledger.stat().st_size,
         "sha256": __import__("hashlib").sha256(ledger.read_bytes()).hexdigest()},
    ]
    archive = tmp_path / "v1.tar.gz"
    metadata = {"format": "market-data-center-backup.v1", "files": entries}
    with tarfile.open(archive, "w:gz") as output:
        output.add(part, arcname=entries[0]["path"])
        output.add(ledger, arcname=entries[1]["path"])
        encoded = __import__("json").dumps(metadata).encode()
        info = tarfile.TarInfo("backup.json")
        info.size = len(encoded)
        output.addfile(info, __import__("io").BytesIO(encoded))
    tracemalloc.start()
    report = restore_backup(archive, tmp_path / "restored", tmp_path / "restored/audit/ledger.sqlite")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert report["format"] == "market-data-center-backup.v1"
    assert peak < 8 * 1024 * 1024


def test_backup_can_enforce_distinct_fault_domain(tmp_path):
    root = tmp_path / "canonical"
    ledger = root / "audit" / "ledger.sqlite"
    ledger.parent.mkdir(parents=True)
    ledger.write_bytes(b"ledger")
    with pytest.raises(ValueError, match="distinct storage device"):
        create_backup(root, ledger, tmp_path / "backup.tar.gz", require_distinct_device=True)


def test_failed_verify_and_restore_write_safe_receipts(tmp_path):
    evidence = tmp_path / "evidence"
    invalid = tmp_path / "invalid.tar.gz"
    invalid.write_bytes(b"not-a-tar")
    with pytest.raises(tarfile.ReadError):
        verify_backup(invalid, evidence_root=evidence)
    with pytest.raises(tarfile.ReadError):
        restore_backup(invalid, tmp_path / "restore", tmp_path / "restore/audit/ledger.sqlite",
                       evidence_root=evidence)
    receipts = [__import__("json").loads(path.read_text()) for path in evidence.rglob("*.json")]
    assert {receipt["action"] for receipt in receipts} == {"backup_verify", "restore"}
    assert all(receipt["result"] == "failed" and receipt["error_category"] == "ReadError"
               for receipt in receipts)


def test_restore_atomic_publish_never_replaces_racing_target(tmp_path, monkeypatch):
    root = tmp_path / "canonical"
    ledger = root / "audit" / "ledger.sqlite"
    part = root / "provider_bars" / "part.parquet"
    ledger.parent.mkdir(parents=True)
    part.parent.mkdir(parents=True)
    ledger.write_bytes(b"ledger")
    part.write_bytes(b"canonical")
    publish_test_part(root, part)
    archive = tmp_path / "backup.tar.gz"
    create_backup(root, ledger, archive)
    restore_root = tmp_path / "restore"
    target = restore_root / "provider_bars" / "part.parquet"

    from data_center import operations

    original = operations._atomic_publish_no_replace

    def racing_publish(temporary, destination):
        if destination == target and not destination.exists():
            destination.write_bytes(b"racing-writer")
        return original(temporary, destination)

    monkeypatch.setattr(operations, "_atomic_publish_no_replace", racing_publish)
    with pytest.raises(ValueError, match="differs"):
        restore_backup(archive, restore_root, restore_root / "audit/ledger.sqlite")
    assert target.read_bytes() == b"racing-writer"
