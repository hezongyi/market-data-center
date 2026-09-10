import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from data_center.evidence import operation_receipt, write_receipt
from data_center.runs.ledger import RunLedger
from data_center.snapshot import ReceiptIndex, build_snapshot


def seed_runs(ledger: RunLedger, count: int) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(ledger.path) as database:
        database.executemany(
            "insert into runs(run_id,payload) values (?,?)",
            ((str(index), json.dumps({
                "run_id": str(index), "job_id": str(index), "dataset_id": "provider_bars",
                "provider": "fixture", "run_scope": "production" if index % 2 else "acceptance",
                "status": "pass" if index % 3 else "failed", "created_at": stamp,
                "started_at": stamp, "finished_at": stamp,
                "error_type": "FixtureError" if index % 3 == 0 else None,
            })) for index in range(count)),
        )


def test_snapshot_is_bounded_and_does_not_traverse_shared_parent(tmp_path, monkeypatch):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    seed_runs(ledger, 10_000)
    index = ReceiptIndex(tmp_path / "evidence")
    monkeypatch.setattr("pathlib.Path.rglob", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unbounded traversal")))
    started = time.monotonic()
    snapshot = build_snapshot(ledger, receipt_index=index, evidence_root=tmp_path / "evidence")
    assert time.monotonic() - started < 0.5
    assert snapshot.metrics["runs_total"] == 10_000
    assert snapshot.metrics["runs_by_scope"]["acceptance"] == 5_000
    assert snapshot.metrics["failures_by_provider"] == {"fixture": 1667}
    assert snapshot.metrics["lifetime_failures_by_provider"] == {"fixture": 3334}


def test_receipt_index_rebuild_is_explicit_and_idempotent(tmp_path):
    root = tmp_path / "evidence"
    directory = root / "operations" / "backup"
    directory.mkdir(parents=True)
    receipt = {"receipt_id": "one", "action": "backup", "result": "pass",
               "completed_at": "2026-09-10T00:00:00+00:00", "details": {"file_count": 2}}
    (directory / "one.json").write_text(json.dumps(receipt))
    index = ReceiptIndex(root)
    assert index.rebuild()["indexed"] == 1
    assert index.rebuild()["indexed"] == 1
    assert index.latest("backup")["reference"] == "operations/backup/one.json"


def test_corrupt_receipt_index_returns_stale_without_filesystem_fallback(tmp_path, monkeypatch):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    root = tmp_path / "evidence"
    index = ReceiptIndex(root)
    index.path.write_bytes(b"not a sqlite database")
    index = ReceiptIndex(root)

    def forbidden_scandir(path):
        raise AssertionError(f"unexpected filesystem fallback: {path}")

    monkeypatch.setattr(os, "scandir", forbidden_scandir)
    snapshot = build_snapshot(ledger, receipt_index=index, evidence_root=root)
    assert snapshot.status == "stale"
    assert snapshot.last_successful_backup_at is None


def test_shared_parent_file_count_does_not_affect_snapshot(tmp_path, monkeypatch):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    index = ReceiptIndex(tmp_path / "evidence")
    shared = tmp_path / "shared-mount"
    shared.mkdir()
    for number in range(1_000):
        (shared / f"unrelated-{number}").write_text("x")
    scanned = []
    original = os.scandir

    def tracked(path):
        scanned.append(Path(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", tracked)
    snapshot = build_snapshot(
        ledger, receipt_index=index, evidence_root=tmp_path / "evidence",
        backup_root=tmp_path / "backups", restore_staging_root=tmp_path / "restore",
    )
    assert snapshot.status == "fresh"
    assert shared not in scanned
    assert not any(shared in path.parents for path in scanned)


def test_ten_thousand_receipt_index_and_metrics_latency(tmp_path):
    root = tmp_path / "evidence"
    directory = root / "operations" / "backup"
    directory.mkdir(parents=True)
    for number in range(10_000):
        receipt = {
            "receipt_id": str(number), "action": "backup", "result": "pass",
            "completed_at": f"2026-09-10T00:{number // 60 % 60:02d}:{number % 60:02d}+00:00",
            "details": {"file_count": number},
        }
        (directory / f"{number:05d}.json").write_text(json.dumps(receipt))
    index = ReceiptIndex(root)
    rebuilt = index.rebuild()
    assert rebuilt["indexed"] == 10_000
    assert index.latest("backup") is not None

    ledger = RunLedger(tmp_path / "ledger.sqlite")
    seed_runs(ledger, 10_000)
    cold_started = time.monotonic()
    build_snapshot(ledger, receipt_index=index)
    assert time.monotonic() - cold_started < 3.0
    warm = []
    for _ in range(20):
        started = time.monotonic()
        build_snapshot(ledger, receipt_index=index)
        warm.append(time.monotonic() - started)
    assert sorted(warm)[18] < 0.5


def test_receipt_index_failure_keeps_source_receipt_and_records_repair_need(tmp_path, monkeypatch):
    started_at = "2026-09-10T00:00:00+00:00"
    receipt = operation_receipt(
        action="backup", command="test", started_at=started_at, result="pass",
    )
    monkeypatch.setattr(
        ReceiptIndex, "add",
        lambda self, payload, reference="": (_ for _ in ()).throw(sqlite3.DatabaseError("corrupt")),
    )

    source = write_receipt(tmp_path, receipt)

    assert source is not None and json.loads(source.read_text())["result"] == "pass"
    repairs = list((tmp_path / "operations" / "receipt_index_repair_needed").glob("*.json"))
    assert len(repairs) == 1
    repair = json.loads(repairs[0].read_text())
    assert repair["failure_stage"] == "index_update"
    assert repair["details"]["source_receipt_id"] == receipt["receipt_id"]
