"""Takeover preparation: capture the legacy timers, import paused plans, compare.

Plan S5.2 steps 1–2 and 6.  These tests run entirely against isolated roots and
unit files: nothing here installs, stops or starts a unit, and the import path is
dry by default.  What is asserted is what an operator has to be able to show
before the real handover: the old scope, the new plans, and the differences.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from data_center.instants import parse_instant
from data_center.production_tasks import ProductionConflict, ProductionTasks
from data_center.runs.ledger import RunLedger
from data_center.takeover import (
    LegacyEntry,
    compare_entries,
    declared_entries,
    entries_from_inventory,
    host_inventory,
    import_entries,
    main,
    plan_definitions,
    planned_entries,
    verify_takeover,
)

REPOSITORY = Path(__file__).resolve().parents[2]
UNIT_ROOT = REPOSITORY / "deploy" / "systemd"
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def host_inventory_payload() -> dict:
    """The host as it actually is: the derived entry is installed but undeclared."""
    return {
        "status": "known",
        "units": ["market-data-center-1m-maintenance.service",
                  "market-data-center-1m-maintenance.timer",
                  "marketlab-market-bars-maintenance.service",
                  "marketlab-market-bars-maintenance.timer"],
        "entries": [{"unit": "marketlab-market-bars-maintenance.service",
                     "timer": "marketlab-market-bars-maintenance.timer",
                     "exec_start": "/opt/marketlab/.venv/bin/python -m data_center.derived_maintenance_runner "
                                   "--provider dukascopy --recipes utc-24x7-1m-to-5m-ohlcv",
                     "cadence_seconds": 900, "cadence_source": "OnUnitInactiveSec"}],
    }


def service(tmp_path) -> ProductionTasks:
    return ProductionTasks(RunLedger(tmp_path / "ledger.sqlite"), canonical_root=tmp_path / "lake")


def test_declared_entries_capture_what_the_repository_ships():
    entries = declared_entries(UNIT_ROOT)
    assert [entry.unit for entry in entries] == ["market-data-center-1m-maintenance.service"]
    entry = entries[0]
    assert "data_center.maintenance_runner" in entry.command
    # The cadence is read from the timer, not guessed from the description.
    assert entry.cadence_seconds == 900 and entry.cadence_source == "OnUnitInactiveSec"
    assert entry.timer == "market-data-center-1m-maintenance.timer"
    assert entry.requires_api is True
    assert "--provider" in entry.argv and entry.argv[entry.argv.index("--provider") + 1] == "dukascopy"


def test_a_host_only_entry_is_captured_and_marked_as_such():
    inventory = host_inventory_payload()
    entries = planned_entries(UNIT_ROOT, inventory)
    assert [entry.unit for entry in entries] == [
        "market-data-center-1m-maintenance.service", "marketlab-market-bars-maintenance.service"]
    comparison = compare_entries(entries, inventory,
                                 declared_units={entries[0].unit})
    host_only = [row for row in comparison["entries"] if row["host_only"]]
    assert [row["unit"] for row in host_only] == ["marketlab-market-bars-maintenance.service"]
    assert all(row["installed"] for row in comparison["entries"])
    assert comparison["declared_units"] == ["market-data-center-1m-maintenance.service"]


def test_host_inventory_reports_unknown_instead_of_guessing():
    inventory = host_inventory(systemctl=("/nonexistent/systemctl-binary",))
    assert inventory["status"] == "unknown" and inventory["units"] == []
    # An unknown host never turns into "installed" or "not installed" claims.
    comparison = compare_entries(declared_entries(UNIT_ROOT), inventory)
    assert comparison["entries"][0]["installed"] is None
    assert comparison["installed_not_declared"] == []


def test_plan_definitions_never_widen_the_legacy_scope():
    """The mapping keeps the old cadence, tail window and output scope."""
    raw, derived = planned_entries(UNIT_ROOT, host_inventory_payload())
    raw_definition = plan_definitions(raw)[0]
    assert raw_definition["provider"] == "dukascopy" and raw_definition["price_basis"] == "bid"
    # The old raw entry produced raw only; the imported plan must not add outputs.
    assert raw_definition["bar_timeframes"] == []
    assert raw_definition["schedule"] == {"schedule": "fixed_delay", "interval_seconds": 900}
    start = parse_instant(raw_definition["window_policy"]["history_start"])
    assert 1 <= (NOW - start).days <= 3, "the plan starts where the legacy tail window started"

    derived_definition = plan_definitions(derived)[0]
    assert derived_definition["bar_timeframes"] == ["5m"]
    # ...and it covers exactly the instruments the provider is approved for.
    assert len(plan_definitions(raw)) == 8


def test_import_is_dry_by_default_and_paused_when_applied(tmp_path):
    entries = planned_entries(UNIT_ROOT)
    tasks = service(tmp_path)
    dry = import_entries(tasks, entries=entries, actor="test", apply=False, now=NOW)
    assert dry["created"] == [] and len(dry["skipped"]) == 8
    assert all(row["reason"] == "dry_run" for row in dry["skipped"])
    assert tasks.ledger.list_production_tasks(include_deleted=True) == []

    applied = import_entries(tasks, entries=entries, actor="test", apply=True, now=NOW)
    assert len(applied["created"]) == 8 and applied["skipped"] == []
    plans = tasks.ledger.list_production_tasks(include_deleted=True)
    # Importing is not enabling: every imported plan starts paused.
    assert {plan["desired_state"] for plan in plans} == {"paused"}
    assert all(plan["payload"]["schedule"]["interval_seconds"] == 900 for plan in plans)
    assert {plan["symbol"] for plan in plans} == {"EURUSD", "GBPUSD", "USDCAD", "USDJPY",
                                                 "AUDJPY", "GBPJPY", "XAUUSD", "BTCUSD"}

    # Re-running is idempotent: the second import creates nothing new.
    again = import_entries(tasks, entries=entries, actor="test", apply=True, now=NOW)
    assert again["created"] == [] and len(again["skipped"]) == 8
    assert all(row["reason"] == "already_imported" for row in again["skipped"])
    assert len(tasks.ledger.list_production_tasks(include_deleted=True)) == 8


def test_verify_lists_every_reason_a_takeover_is_not_finished(tmp_path):
    entries = planned_entries(UNIT_ROOT, host_inventory_payload())
    tasks = service(tmp_path)
    inventory = host_inventory_payload()

    before = verify_takeover(tasks, entries=entries, inventory=inventory)
    assert any("was never imported" in problem for problem in before["problems"])
    assert any("still installed" in problem for problem in before["problems"])

    import_entries(tasks, entries=entries, actor="test", apply=True, now=NOW)
    # The legacy units are still running, so the handover is still not verified.
    still_installed = verify_takeover(tasks, entries=entries, inventory=inventory)
    assert still_installed["problems"] == [
        ("legacy units are still installed: market-data-center-1m-maintenance.service, "
         "marketlab-market-bars-maintenance.service"),
    ]
    assert {row["desired_state"] for row in still_installed["plans"]} == {"paused"}

    # Once the host no longer has them, the post-conditions hold.
    clean_inventory = {**inventory, "units": []}
    verified = verify_takeover(tasks, entries=entries, inventory=clean_inventory)
    assert verified["problems"] == []
    # Two legacy entries, one plan per instrument: raw and derived are one scope.
    assert len(verified["plans"]) == 8
    assert all(row["desired_state"] == "paused" for row in verified["plans"])


def test_verify_reports_two_writers_for_one_output(tmp_path):
    entries = planned_entries(UNIT_ROOT)
    tasks = service(tmp_path)
    import_entries(tasks, entries=entries, actor="test", apply=True, now=NOW)
    # A plan that was enabled while the legacy units are still running is exactly
    # the double-scheduling state this check exists to catch.
    definition = plan_definitions(entries[0])[0]
    with pytest.raises(ProductionConflict) as conflict:
        tasks.create(definition=definition, name="duplicate", task_id="duplicate",
                     desired_state="enabled", now=NOW)
    assert conflict.value.code == "ownership_conflict"
    report = verify_takeover(tasks, entries=entries, inventory={"status": "known", "units": []})
    assert report["problems"] == []
    assert all(row["desired_state"] == "paused" for row in report["plans"])


def test_cli_plan_and_import_never_write_without_apply(tmp_path, monkeypatch, capsys):
    ledger_path = tmp_path / "ledger.sqlite"
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(host_inventory_payload()))
    monkeypatch.setenv("DATACENTER_LEDGER_PATH", str(ledger_path))
    monkeypatch.setenv("DATACENTER_CANONICAL_ROOT", str(tmp_path / "lake"))
    monkeypatch.setenv("DATACENTER_EVIDENCE_ROOT", str(tmp_path / "evidence"))

    assert main(["plan", "--unit-root", str(UNIT_ROOT), "--inventory", str(inventory_path)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert [row["unit"] for row in plan["entries"]] == [
        "market-data-center-1m-maintenance.service", "marketlab-market-bars-maintenance.service"]
    assert not ledger_path.exists(), "a plan is a read: it must not create a ledger"

    assert main(["import", "--unit-root", str(UNIT_ROOT), "--inventory", str(inventory_path)]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["applied"] is False and dry["created"] == 0 and dry["skipped"] == 8
    assert RunLedger(ledger_path).list_production_tasks(include_deleted=True) == []

    assert main(["import", "--unit-root", str(UNIT_ROOT), "--inventory", str(inventory_path),
                 "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["created"] == 8
    receipts = list((tmp_path / "evidence" / "operations" / "production_takeover_import").glob("*.json"))
    assert receipts, "every import writes a receipt, applied or not"

    # With the legacy units still installed, verification is expected to fail.
    assert main(["verify", "--unit-root", str(UNIT_ROOT), "--inventory", str(inventory_path)]) == 1


def test_entries_from_inventory_ignores_units_that_are_not_legacy_runners():
    payload = {"entries": [{"unit": "other.service", "exec_start": "/bin/true"},
                           {"unit": "legacy.service",
                            "exec_start": "python -m data_center.maintenance_runner --provider dukascopy",
                            "cadence_seconds": 600}]}
    entries = entries_from_inventory(payload)
    assert [entry.unit for entry in entries] == ["legacy.service"]
    assert entries[0].cadence_seconds == 600 and entries[0].cadence_source == "host_inventory"
    assert isinstance(entries[0], LegacyEntry)
