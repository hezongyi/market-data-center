from __future__ import annotations

import sqlite3

from data_center.runs.ledger import RunLedger


def test_ledger_initializes_versioned_wal_schema_and_batch_is_atomic(tmp_path):
    path = tmp_path / "ledger.sqlite"
    clock = lambda: 1_700_000_000.0
    ledger = RunLedger(path, clock=clock)
    payloads = [
        {"job_id": "task-a-1", "dataset_id": "provider_bars", "owner_plan_id": "plan-a"},
        {"job_id": "task-a-2", "dataset_id": "provider_bars", "owner_plan_id": "plan-a"},
    ]
    run_ids = ledger.enqueue_batch(payloads)
    assert len(run_ids) == 2
    assert all(ledger.get(run_id)["created_at"] == "2023-11-14T22:13:20+00:00" for run_id in run_ids)
    with sqlite3.connect(path) as conn:
        assert conn.execute("pragma user_version").fetchone()[0] == 1
        assert conn.execute("pragma journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("select count(*) from jobs where owner_plan_id='plan-a'").fetchone()[0] == 2


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "ledger.sqlite"
    RunLedger(path)
    RunLedger(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("pragma user_version").fetchone()[0] == 1
        assert conn.execute("select count(*) from schema_migrations").fetchone()[0] == 1


def test_claim_uses_persisted_plan_ownership_for_pause(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    ledger.upsert_maintenance_task("plan-a", {"name": "A"}, status="paused")
    ledger.enqueue_job({"job_id": "unrelated-id", "dataset_id": "provider_bars", "owner_plan_id": "plan-a"})
    assert ledger.claim_next_job() is None
