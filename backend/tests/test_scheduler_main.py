"""The scheduler process: bounded ticks, receipts, and a clean stop.

The process is deliberately thin.  These tests cover what it adds beyond the
tick itself: a durable receipt per tick, shadow mode as the default, and a stop
signal that ends the loop instead of being ignored.
"""
from __future__ import annotations

import json
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from data_center.production_tasks import ProductionTasks
from data_center.runs.ledger import RunLedger
from data_center.scheduler import Scheduler
from data_center.scheduler_main import main, run_tick
from data_center.settings import Settings

REPOSITORY = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def settings(tmp_path, **overrides) -> Settings:
    return Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "ledger.sqlite",
                    evidence_root=tmp_path / "evidence", **overrides)


def plan(ledger, *, next_run_at="2026-09-14T11:59:00+00:00", state="enabled") -> None:
    ledger.create_production_task(
        task_id="p1", name="EURUSD", provider="dukascopy", symbol="EURUSD",
        payload={"schedule": {"schedule": "fixed_rate", "interval_seconds": 900,
                              "anchor": "2026-09-14T12:00:00+00:00"}},
        ownership_keys=["provider_bars:dukascopy:EURUSD:1m:bid"], desired_state=state,
        next_run_at=next_run_at)


def test_shadow_tick_writes_a_receipt_without_dispatching(tmp_path):
    config = settings(tmp_path)
    ledger = RunLedger(config.ledger_path)
    plan(ledger)
    scheduler = Scheduler(ledger, instance_id="one")
    result = run_tick(scheduler, config, now=NOW)
    assert result["receipt"] is not None
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert receipt["receipt_version"] == "operational-receipt.v1"
    assert receipt["action"] == "scheduler_tick"
    assert receipt["result"] == "pass"
    assert receipt["details"]["dispatch_enabled"] is False
    assert receipt["details"]["evaluated"] == 1
    assert receipt["details"]["decisions"][0]["action"] == "shadow_start_execution"
    # Shadow mode observes: it must not create an execution or claim a slot.
    assert ledger.list_production_executions("p1") == []
    assert ledger.get_production_task("p1")["next_run_at"] == "2026-09-14T11:59:00+00:00"


def test_dispatch_tick_records_the_claimed_execution(tmp_path):
    config = settings(tmp_path)
    ledger = RunLedger(config.ledger_path)
    plan(ledger)
    result = run_tick(Scheduler(ledger, instance_id="one", dispatch_enabled=True), config, now=NOW)
    assert result["dispatched"] == 1
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert receipt["details"]["dispatched"] == 1
    assert ledger.list_production_executions("p1")[0]["state"] == "pending"


def test_once_runs_a_single_tick_and_exits(tmp_path, monkeypatch):
    config = settings(tmp_path)
    ledger = RunLedger(config.ledger_path)
    plan(ledger)
    monkeypatch.setenv("DATACENTER_LEDGER_PATH", str(config.ledger_path))
    monkeypatch.setenv("DATACENTER_CANONICAL_ROOT", str(config.canonical_root))
    monkeypatch.setenv("DATACENTER_EVIDENCE_ROOT", str(config.evidence_root))
    monkeypatch.setenv("DATACENTER_SCHEDULER_INTERVAL_SECONDS", "0.01")
    assert main(["--once"]) == 0
    receipts = list((config.evidence_root / "operations" / "scheduler_tick").glob("*.json"))
    assert len(receipts) == 1


def test_stop_signal_ends_the_loop(tmp_path, monkeypatch):
    config = settings(tmp_path)
    RunLedger(config.ledger_path)
    monkeypatch.setenv("DATACENTER_LEDGER_PATH", str(config.ledger_path))
    monkeypatch.setenv("DATACENTER_CANONICAL_ROOT", str(config.canonical_root))
    monkeypatch.setenv("DATACENTER_EVIDENCE_ROOT", str(config.evidence_root))
    monkeypatch.setenv("DATACENTER_SCHEDULER_INTERVAL_SECONDS", "5")

    import data_center.scheduler_main as module

    def stop_on_first_tick(*args, **kwargs):
        module._request_stop(signal.SIGTERM, None)
        return {"instance_id": "one", "dispatch_enabled": False, "evaluated": 0, "decisions": [],
                "tick_at": NOW.isoformat(), "tick_seconds": 0.0}

    monkeypatch.setattr(module, "run_tick", stop_on_first_tick)
    assert module.main([]) == 0


def test_process_starts_reports_identity_and_stops(tmp_path):
    """The module runs as a process and answers SIGTERM without a traceback."""
    ledger_path = tmp_path / "ledger.sqlite"
    RunLedger(ledger_path)
    process = subprocess.Popen(
        [sys.executable, "-m", "data_center.scheduler_main", "--interval-seconds", "5"],
        cwd=REPOSITORY,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPOSITORY / "backend" / "src"),
             "DATACENTER_LEDGER_PATH": str(ledger_path),
             "DATACENTER_CANONICAL_ROOT": str(tmp_path / "lake"),
             "DATACENTER_EVIDENCE_ROOT": str(tmp_path / "evidence")},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        started = json.loads(process.stdout.readline())
        assert started["event"] == "scheduler_started"
        assert started["dispatch_enabled"] is False
        tick = json.loads(process.stdout.readline())
        assert tick["event"] == "scheduler_tick" and tick["evaluated"] == 0
        process.send_signal(signal.SIGTERM)
        # communicate() also closes the pipes, so no handle is left dangling.
        remaining, errors = process.communicate(timeout=30)
        assert process.returncode == 0, errors
        assert json.loads(remaining.splitlines()[-1])["event"] == "scheduler_stopped"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


def test_settings_default_to_shadow_dispatch():
    assert Settings().scheduler_dispatch_enabled is False
    assert Settings().scheduler_interval_seconds == 15.0
    assert Settings().scheduler_tick_budget == 50


def test_production_tasks_module_is_the_only_plan_writer(tmp_path):
    """The scheduler reads plans through the ledger and never edits definitions."""
    config = settings(tmp_path)
    ledger = RunLedger(config.ledger_path)
    service = ProductionTasks(ledger)
    service.create(definition={"provider": "dukascopy", "symbol": "EURUSD", "bar_timeframes": [],
                               "window_policy": {"mode": "continuous",
                                                 "history_start": "2026-01-01T00:00:00+00:00"},
                               "schedule": {"schedule": "fixed_rate", "interval_seconds": 900,
                                            "anchor": "2026-09-14T12:00:00+00:00"}},
                   name="EURUSD", task_id="p1", now=NOW)
    before = ledger.get_production_task("p1")["definition_version"]
    run_tick(Scheduler(ledger, instance_id="one", dispatch_enabled=True), config, now=NOW)
    assert ledger.get_production_task("p1")["definition_version"] == before
