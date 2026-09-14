from __future__ import annotations

import sqlite3

from data_center.runs.ledger import RunLedger


def test_ledger_initializes_versioned_wal_schema_and_batch_is_atomic(tmp_path):
    path = tmp_path / "ledger.sqlite"
    def clock():
        return 1_700_000_000.0
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


def test_production_task_ownership_is_atomic_and_unique(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    task = ledger.create_production_task(task_id="p1", name="EURUSD", alias="eurusd",
                                         payload={"provider": "fixture"}, ownership_keys=["provider_bars:fixture:EURUSD:1m"],
                                         desired_state="paused")
    assert task["definition_version"] == 1
    assert ledger.list_production_tasks()[0]["task_id"] == "p1"
    try:
        ledger.create_production_task(task_id="p2", name="Other", payload={},
                                      ownership_keys=["provider_bars:fixture:EURUSD:1m"])
    except ValueError:
        pass
    else:
        raise AssertionError("ownership conflict must be rejected")


def test_production_task_pause_archive_delete_retains_tombstone(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    ledger.create_production_task(task_id="p1", name="A", payload={}, ownership_keys=["k"], desired_state="enabled")
    try:
        ledger.delete_production_task("p1")
    except ValueError:
        pass
    ledger.set_production_task_state("p1", "paused")
    ledger.set_production_task_state("p1", "archived")
    tombstone = ledger.delete_production_task("p1")
    assert tombstone["task_id"] == "p1"
    assert ledger.list_production_tasks() == []
    assert ledger.list_production_tasks(include_deleted=True)[0]["deleted_at"]


def test_production_task_update_uses_optimistic_version(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    ledger.create_production_task(task_id="p1", name="A", payload={"x": 1}, ownership_keys=["k"])
    updated = ledger.update_production_task("p1", {"x": 2}, expected_version=1)
    assert updated["definition_version"] == 2
    try:
        ledger.update_production_task("p1", {"x": 3}, expected_version=1)
    except ValueError:
        pass
    else:
        raise AssertionError("stale definition version must be rejected")
