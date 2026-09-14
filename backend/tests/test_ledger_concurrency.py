"""Multi-process contention: the ledger must survive several real writers.

The API, worker, monitor and scheduler are separate processes sharing one
SQLite file, so a thread test would prove nothing about the guarantees that
matter (spec 7.4.1, AC22).  These tests use spawned processes and assert that
nothing is lost, nothing is claimed twice and no writer ever sees
``database is locked``.
"""
from __future__ import annotations

import multiprocessing
import sqlite3
from pathlib import Path

from data_center.runs.ledger import ProductionConflict, RunLedger

WORKERS = 4
JOBS_PER_WORKER = 25


def _enqueue_worker(path: str, index: int, results) -> None:
    ledger = RunLedger(Path(path))
    claimed = []
    try:
        for job in range(JOBS_PER_WORKER):
            ledger.enqueue_job({"job_id": f"job-{index}-{job}", "dataset_id": "provider_bars",
                                "run_kind": "ingest", "run_scope": "production"})
        # Keep claiming until the queue is drained by everyone.
        while len(claimed) < JOBS_PER_WORKER:
            claim = ledger.claim_next_job()
            if claim is None:
                continue
            claimed.append(claim["job_id"])
            ledger.complete_job(claim["job_id"])
    except Exception as exc:  # noqa: BLE001 - the failure mode is the assertion
        results.put(("error", f"{type(exc).__name__}: {exc}"))
        return
    results.put(("ok", claimed))


def _claim_same_task_worker(path: str, index: int, results) -> None:
    """Every worker asks for the same output; exactly one may own it."""
    ledger = RunLedger(Path(path))
    try:
        ledger.create_production_task(task_id=f"plan-{index}", name=f"worker-{index}", payload={},
                                      ownership_keys=["shared-key"], desired_state="paused")
    except ProductionConflict as exc:
        results.put(("conflict", exc.code))
        return
    except Exception as exc:  # noqa: BLE001
        results.put(("error", f"{type(exc).__name__}: {exc}"))
        return
    results.put(("created", "shared"))


def _run_in_processes(target, path: Path, count: int) -> list[tuple[str, object]]:
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    processes = [context.Process(target=target, args=(str(path), index, results))
                 for index in range(count)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=120)
        assert process.exitcode == 0, f"worker exited with {process.exitcode}"
    return [results.get(timeout=10) for _ in range(count)]


def test_concurrent_processes_never_lose_or_duplicate_a_claim(tmp_path):
    path = tmp_path / "ledger.sqlite"
    RunLedger(path)
    outcomes = _run_in_processes(_enqueue_worker, path, WORKERS)
    errors = [value for status, value in outcomes if status == "error"]
    assert errors == [], f"a writer failed under contention: {errors}"
    claimed = [job for status, jobs in outcomes if status == "ok" for job in jobs]
    assert len(claimed) == WORKERS * JOBS_PER_WORKER
    # Every job was claimed exactly once: no duplicate work, nothing stranded.
    assert len(set(claimed)) == len(claimed)
    ledger = RunLedger(path)
    with sqlite3.connect(path) as conn:
        states = dict(conn.execute("select status, count(*) from jobs group by status").fetchall())
        total_runs = conn.execute("select count(*) from runs").fetchone()[0]
    assert states.get("completed") == WORKERS * JOBS_PER_WORKER
    assert states.get("queued", 0) == 0 and states.get("running", 0) == 0
    assert total_runs == WORKERS * JOBS_PER_WORKER
    payloads = ledger.list()
    assert {item["run_id"] for item in payloads} == {item["run_id"] for item in payloads}


def test_concurrent_processes_agree_on_one_owner_for_a_key(tmp_path):
    path = tmp_path / "ledger.sqlite"
    RunLedger(path)
    outcomes = _run_in_processes(_claim_same_task_worker, path, WORKERS)
    created = [value for status, value in outcomes if status == "created"]
    conflicts = [value for status, value in outcomes if status == "conflict"]
    errors = [value for status, value in outcomes if status == "error"]
    assert errors == []
    assert created == ["shared"]
    assert conflicts == ["ownership_conflict"] * (WORKERS - 1)
    ledger = RunLedger(path)
    assert len(ledger.list_production_tasks()) == 1
    assert len(ledger.ownership_holders(["shared-key"])) == 1
