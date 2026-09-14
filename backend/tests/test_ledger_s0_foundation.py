from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from data_center.runs.ledger import SCHEMA_VERSION, RunLedger


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
        assert conn.execute("pragma user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("pragma journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("select count(*) from jobs where owner_plan_id='plan-a'").fetchone()[0] == 2


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "ledger.sqlite"
    RunLedger(path)
    RunLedger(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("pragma user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("select count(*) from schema_migrations").fetchone()[0] == SCHEMA_VERSION


def test_ledger_refuses_a_schema_written_by_a_newer_binary(tmp_path):
    path = tmp_path / "ledger.sqlite"
    RunLedger(path)
    with sqlite3.connect(path) as conn:
        conn.execute(f"pragma user_version={SCHEMA_VERSION + 1}")
        conn.commit()
    try:
        RunLedger(path)
    except RuntimeError as exc:
        assert "newer than supported" in str(exc)
    else:
        raise AssertionError("a future schema version must not be opened")


def test_unversioned_production_ledger_upgrades_in_place(tmp_path):
    """A ledger written before migrations were versioned still upgrades."""
    path = tmp_path / "ledger.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("create table runs (run_id text primary key, payload text not null)")
        conn.execute("create table jobs (job_id text primary key, run_id text not null, status text not null, payload text not null)")
        conn.execute("insert into runs(run_id, payload) values ('r1', '{\"run_id\": \"r1\"}')")
        conn.execute("pragma user_version=0")
        conn.commit()
    ledger = RunLedger(path)
    assert ledger.schema_version() == SCHEMA_VERSION
    assert ledger.get("r1")["run_id"] == "r1"
    with sqlite3.connect(path) as conn:
        assert conn.execute("select count(*) from schema_migrations").fetchone()[0] == SCHEMA_VERSION
        columns = {row[1] for row in conn.execute("pragma table_info(jobs)")}
        assert {"owner_plan_id", "owner_step_id", "owner_execution_id"} <= columns


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


def test_execution_and_steps_are_durable(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    ledger.create_production_task(task_id="p", name="A", payload={}, ownership_keys=["k"])
    execution = ledger.create_production_execution(execution_id="e", task_id="p", definition_version=1, trigger_source="manual")
    ledger.add_production_step(step_id="s", execution_id="e", stage="raw", window_start="a", window_end="b")
    assert ledger.list_production_executions("p")[0]["execution_id"] == execution["execution_id"]
    assert ledger.list_production_steps("e")[0]["stage"] == "raw"


def test_one_instant_parser_accepts_what_every_supported_python_accepts():
    """AC04/AC19: the console submits ``Date.toISOString()``; 3.10 must read it.

    ``datetime.fromisoformat`` only learned the ``Z`` suffix in 3.11, so the
    scheduler and the plan API used to reject the console's own instants on the
    interpreter hosted CI still runs.  One parser owns that difference.
    """
    from data_center.instants import aware_utc, optional_instant, parse_instant

    moment = datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc)
    # The three spellings of the same instant must agree.
    assert parse_instant("2026-09-14T11:00:00Z") == moment
    assert parse_instant("2026-09-14T11:00:00z") == moment
    assert parse_instant("2026-09-14T11:00:00+00:00") == moment
    # An aware datetime passes through unchanged; a naive one is never guessed.
    assert parse_instant(moment) is moment
    assert optional_instant(None) is None
    assert aware_utc("2026-09-14T13:00:00+02:00") == moment
    with pytest.raises(ValueError, match="timezone"):
        aware_utc("2026-09-14T11:00:00")
    # A malformed instant is a validation error, not a silent guess.
    with pytest.raises(ValueError):
        parse_instant("not-an-instant")
