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
    declared_unit_files,
    default_unit_root,
    entries_from_inventory,
    host_inventory,
    import_entries,
    legacy_symbols,
    main,
    merged_definitions,
    plan_definitions,
    planned_entries,
    schedule_for,
    schedule_seconds,
    verify_takeover,
)

REPOSITORY = Path(__file__).resolve().parents[2]
UNIT_ROOT = REPOSITORY / "deploy" / "systemd"
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
#: The machine-level allowlist the legacy 1m unit relied on (spec 4.2).
LEGACY_SYMBOLS = ("EURUSD", "GBPUSD", "USDCAD", "USDJPY", "AUDJPY", "GBPJPY", "XAUUSD", "BTCUSD")


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
                                   "--provider dukascopy --recipes utc-24x7-1m-to-5m-ohlcv@1",
                     "cadence_seconds": 900, "cadence_source": "OnUnitInactiveSec"}],
    }


def _tmp_ledger_path() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp()) / "ledger.sqlite"


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
    raw_definition = plan_definitions(raw, maintenance_symbols=LEGACY_SYMBOLS, now=NOW)[0]
    assert raw_definition["provider"] == "dukascopy" and raw_definition["price_basis"] == "bid"
    # The old raw entry produced raw only; the imported plan must not add outputs.
    assert raw_definition["bar_timeframes"] == []
    assert raw_definition["schedule"] == {"schedule": "fixed_delay", "interval_seconds": 900}
    start = parse_instant(raw_definition["window_policy"]["history_start"])
    assert 1 <= (NOW - start).days <= 3, "the plan starts where the legacy tail window started"

    # The unit names its recipe as id@version, which is how the host spells it.
    derived_definition = plan_definitions(derived, maintenance_symbols=LEGACY_SYMBOLS, now=NOW)[0]
    assert derived_definition["bar_timeframes"] == ["5m"]
    # ...and it covers exactly the instruments the legacy allowlist named.
    assert len(plan_definitions(raw, maintenance_symbols=LEGACY_SYMBOLS, now=NOW)) == 8


def test_a_two_level_wrapper_chain_reveals_the_runner_scope(tmp_path):
    """The real macro entry execs a second script: following it reads the scope.

    Reading one level found the wrapper but not the runner, so the derived half of
    the takeover could only be imported by hand-editing an inventory file.
    """
    other = tmp_path / "macro-market-lab"
    (other / "scripts").mkdir(parents=True)
    outer = other / "scripts" / "marketlab-maintain-market-bars.sh"
    inner = other / "scripts" / "marketlab-maintain-market-bars-data-center.sh"
    outer.write_text(f"""#!/usr/bin/env bash
REPO_ROOT="${{MARKETLAB_REPO_ROOT:-{other}}}"
if [[ "${{MARKETLAB_MARKET_BARS_BACKEND:-legacy}}" == "data_center" ]]; then
  exec "${{REPO_ROOT}}/scripts/marketlab-maintain-market-bars-data-center.sh"
fi
""")
    inner.write_text("""#!/usr/bin/env bash
DATA_CENTER_PYTHON="${MARKETLAB_DATA_CENTER_PYTHON:-/opt/dc/.venv/bin/python}"
SYMBOLS="${MARKETLAB_DATA_CENTER_SYMBOLS:-XAUUSD BTCUSDT}"
START="${MARKETLAB_DATA_CENTER_START:-$(date -u -d '2 days ago' +%FT00:00:00Z)}"
exec "${DATA_CENTER_PYTHON}" -m data_center.derived_maintenance_runner \\
  --provider dukascopy \\
  --symbols ${SYMBOLS} \\
  --recipes utc-24x7-1m-to-5m-ohlcv@1 \\
  --start "${START}" --run-scope production
""")
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(f"""#!/bin/sh
if [ "$1" = "list-unit-files" ]; then
  echo "marketlab-market-bars-maintenance.service static"
  exit 0
fi
if [ "$1" = "show" ]; then
  case "$2" in
    marketlab-market-bars-maintenance.service)
      case "$4" in
        Environment) echo "MARKETLAB_REPO_ROOT={other} MARKETLAB_MARKET_BARS_BACKEND=data_center" ;;
        ExecStart) echo "{{ path=/bin/sh ; argv[]=/bin/sh {outer} ; }}" ;;
        *) echo "" ;;
      esac ;;
    marketlab-market-bars-maintenance.timer)
      echo "{{ OnCalendar=*-*-* 06:30:00 UTC }}" ;;
    *) echo "" ;;
  esac
  exit 0
fi
exit 0
""")
    systemctl.chmod(0o755)
    inventory = host_inventory(systemctl=(str(systemctl),))
    entry = entries_from_inventory(inventory)[0]

    assert entry.runner_module == "data_center.derived_maintenance_runner"
    # The chain is followed to the runner's own command: its arguments survive, the
    # literal allowlist default is readable, and the date substitution is left as
    # it is instead of being invented.
    assert "utc-24x7-1m-to-5m-ohlcv@1" in entry.command
    assert legacy_symbols(entry) == ["BTCUSDT", "XAUUSD"]
    definitions = plan_definitions(entry, now=NOW)
    # BTCUSDT is binance's, so the dukascopy plan covers XAUUSD with its 5m output
    # and the mismatch is evidence in the receipt instead of a silent narrowing.
    assert [(item["symbol"], item["bar_timeframes"]) for item in definitions] == [("XAUUSD", ["5m"])]
    comparison = compare_entries([entry], inventory, declared_units=set())
    row = comparison["entries"][0]
    assert row["importable_symbols"] == ["XAUUSD"] and row["unserved_symbols"] == ["BTCUSDT"]
    assert comparison["partially_served"] == ["marketlab-market-bars-maintenance.service"]
    assert comparison["unmappable"] == []
    assert row["calendar_zone_assumed"] is False
    assert "wrapper" not in entry.command


def test_the_environment_marker_alone_still_captures_the_entry(tmp_path):
    """A wrapper whose runner cannot be read is captured through its unit environment."""
    script = tmp_path / "opaque.sh"
    script.write_text("#!/bin/sh\nmarketlab data-center\n")
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(f"""#!/bin/sh
if [ "$1" = "list-unit-files" ]; then
  echo "marketlab-market-bars-maintenance.service static"
  exit 0
fi
if [ "$1" = "show" ]; then
  case "$2" in
    marketlab-market-bars-maintenance.service)
      case "$4" in
        Environment) echo "MARKETLAB_MARKET_BARS_BACKEND=data_center" ;;
        ExecStart) echo "{{ path=/bin/sh ; argv[]=/bin/sh {script} ; }}" ;;
        *) echo "" ;;
      esac ;;
    marketlab-market-bars-maintenance.timer)
      echo "{{ OnCalendar=*-*-* 06:30:00 UTC }}" ;;
    *) echo "" ;;
  esac
  exit 0
fi
exit 0
""")
    systemctl.chmod(0o755)
    entry = entries_from_inventory(host_inventory(systemctl=(str(systemctl),)))[0]
    # The unit environment names the integration, so the entry is captured as that
    # producer — but the wrapper names no scope and no recipes, so the operator
    # still has to supply them instead of the tool inventing a plan.
    assert entry.runner_module == "data_center.derived_maintenance_runner"
    assert entry.command == f"/bin/sh {script}"
    assert legacy_symbols(entry) is None
    assert plan_definitions(entry, now=NOW) == []
    # Without the marker this unit is not this platform's producer at all.
    assert entries_from_inventory({"status": "known", "units": [], "entries": [
        {"unit": "opaque.service", "exec_start": f"/bin/sh {script}"}]}) == []


def test_a_partially_served_allowlist_keeps_the_mismatch_as_evidence(monkeypatch):
    """Known, approved and unavailable symbols are three different findings."""
    from types import SimpleNamespace

    from data_center import takeover as module

    instruments = [SimpleNamespace(symbol="EURUSD", approved=True),
                   SimpleNamespace(symbol="XAUUSD", approved=False)]
    monkeypatch.setattr(module.REGISTRY, "instruments",
                        lambda provider, approved_only=True: tuple(instruments))
    scope = module.provider_symbols("dukascopy", ["EURUSD", "XAUUSD", "BTCUSDT"])
    assert scope.served == ("EURUSD",) and scope.unapproved == ("XAUUSD",)
    assert scope.unserved == ("BTCUSDT",)
    assert scope.as_dict()["importable_symbols"] == ["EURUSD"]


def test_a_declared_unit_is_not_reported_as_undeclared():
    """The governance list compares against the repository's own unit files."""
    inventory = {"status": "known",
                 "units": ["market-data-center-api.service", "market-data-center-worker.service",
                           "market-data-center-monitor.timer", "dbus.service"],
                 "entries": []}
    comparison = compare_entries([], inventory, declared_units=set(),
                                 declared_unit_files=declared_unit_files(UNIT_ROOT))
    assert comparison["installed_not_declared"] == []
    # Without the declared file list, declared units would look undeclared.
    partial = compare_entries([], inventory, declared_units=set())
    assert "market-data-center-api.service" in partial["installed_not_declared"]


def test_a_daily_plan_that_drifted_its_wall_clock_time_does_not_verify(tmp_path):
    """The handover must not accept a daily plan that moved its local time."""
    entry = LegacyEntry(unit="derived.service", timer="derived.timer", command="python -m "
                        "data_center.derived_maintenance_runner --provider dukascopy --symbols XAUUSD "
                        "--recipes utc-24x7-1m-to-5m-ohlcv@1",
                        argv=("python", "-m", "data_center.derived_maintenance_runner", "--provider",
                              "dukascopy", "--symbols", "XAUUSD", "--recipes",
                              "utc-24x7-1m-to-5m-ohlcv@1"),
                        cadence_seconds=None, cadence_source="OnCalendar", requires_api=False,
                        calendar="*-*-* 06:30:00 UTC")
    tasks = service(tmp_path)
    definition = merged_definitions([entry], now=NOW)[0]
    task_id = f"legacy-{definition['provider']}-{definition['symbol']}-{definition['raw_timeframe']}"
    tasks.create(definition=definition, name="XAUUSD", task_id=task_id)
    report = verify_takeover(tasks, entries=[entry], inventory={"status": "unknown"}, now=NOW)
    assert report["problems"] == []
    assert report["plans"][0]["schedule"] == {"schedule": "daily", "timezone": "UTC",
                                              "local_time": "06:30"}

    # The operator moves the wall-clock time: the same cadence, a different plan.
    tasks.change(task_id, "update",
                 definition={**definition,
                             "schedule": {"schedule": "daily", "timezone": "UTC",
                                          "local_time": "07:00"}},
                 expected_version=tasks.ledger.get_production_task(task_id)["definition_version"])
    drifted = verify_takeover(tasks, entries=[entry], inventory={"status": "unknown"}, now=NOW)
    assert any("does not match the legacy" in problem for problem in drifted["problems"])


def test_import_is_dry_by_default_and_paused_when_applied(tmp_path):
    entries = planned_entries(UNIT_ROOT)
    tasks = service(tmp_path)
    dry = import_entries(tasks, entries=entries, actor="test", apply=False,
                         maintenance_symbols=LEGACY_SYMBOLS, now=NOW)
    assert dry["created"] == [] and len(dry["skipped"]) == 8
    assert all(row["reason"] == "dry_run" for row in dry["skipped"])
    assert tasks.ledger.list_production_tasks(include_deleted=True) == []

    applied = import_entries(tasks, entries=entries, actor="test", apply=True,
                             maintenance_symbols=LEGACY_SYMBOLS, now=NOW)
    assert len(applied["created"]) == 8 and applied["skipped"] == []
    plans = tasks.ledger.list_production_tasks(include_deleted=True)
    # Importing is not enabling: every imported plan starts paused.
    assert {plan["desired_state"] for plan in plans} == {"paused"}
    assert all(plan["payload"]["schedule"]["interval_seconds"] == 900 for plan in plans)
    assert {plan["symbol"] for plan in plans} == {"EURUSD", "GBPUSD", "USDCAD", "USDJPY",
                                                 "AUDJPY", "GBPJPY", "XAUUSD", "BTCUSD"}

    # Re-running is idempotent: the second import creates nothing new.
    again = import_entries(tasks, entries=entries, actor="test", apply=True,
                           maintenance_symbols=LEGACY_SYMBOLS, now=NOW)
    assert again["created"] == [] and len(again["skipped"]) == 8
    assert all(row["reason"] == "already_imported" for row in again["skipped"])
    assert len(tasks.ledger.list_production_tasks(include_deleted=True)) == 8


def test_verify_lists_every_reason_a_takeover_is_not_finished(tmp_path):
    entries = planned_entries(UNIT_ROOT, host_inventory_payload())
    tasks = service(tmp_path)
    inventory = host_inventory_payload()

    before = verify_takeover(tasks, entries=entries, inventory=inventory,
                             maintenance_symbols=LEGACY_SYMBOLS, now=NOW)
    assert any("was never imported" in problem for problem in before["problems"])
    assert any("still installed" in problem for problem in before["problems"])

    import_entries(tasks, entries=entries, actor="test", apply=True,
                   maintenance_symbols=LEGACY_SYMBOLS, now=NOW)
    # The legacy units are still running, so the handover is still not verified.
    still_installed = verify_takeover(tasks, entries=entries, inventory=inventory,
                                      maintenance_symbols=LEGACY_SYMBOLS, now=NOW)
    assert still_installed["problems"] == [
        ("legacy units are still installed: market-data-center-1m-maintenance.service, "
         "marketlab-market-bars-maintenance.service"),
    ]
    assert {row["desired_state"] for row in still_installed["plans"]} == {"paused"}

    # Once the host no longer has them, the post-conditions hold.
    clean_inventory = {**inventory, "units": []}
    verified = verify_takeover(tasks, entries=entries, inventory=clean_inventory,
                               maintenance_symbols=LEGACY_SYMBOLS, now=NOW)
    assert verified["problems"] == []
    # Two legacy entries, one plan per instrument: raw and derived are one scope.
    assert len(verified["plans"]) == 8
    assert all(row["desired_state"] == "paused" for row in verified["plans"])


def test_verify_reports_two_writers_for_one_output(tmp_path):
    entries = planned_entries(UNIT_ROOT)
    tasks = service(tmp_path)
    import_entries(tasks, entries=entries, actor="test", apply=True,
                   maintenance_symbols=LEGACY_SYMBOLS, now=NOW)
    # A plan that was enabled while the legacy units are still running is exactly
    # the double-scheduling state this check exists to catch.
    definition = plan_definitions(entries[0], maintenance_symbols=LEGACY_SYMBOLS, now=NOW)[0]
    with pytest.raises(ProductionConflict) as conflict:
        tasks.create(definition=definition, name="duplicate", task_id="duplicate",
                     desired_state="enabled", now=NOW)
    assert conflict.value.code == "ownership_conflict"
    report = verify_takeover(tasks, entries=entries, inventory={"status": "known", "units": []},
                             maintenance_symbols=LEGACY_SYMBOLS, now=NOW)
    assert report["problems"] == []
    assert all(row["desired_state"] == "paused" for row in report["plans"])


def test_cli_plan_and_import_never_write_without_apply(tmp_path, monkeypatch, capsys):
    ledger_path = tmp_path / "ledger.sqlite"
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(host_inventory_payload()))
    monkeypatch.setenv("DATACENTER_LEDGER_PATH", str(ledger_path))
    monkeypatch.setenv("DATACENTER_CANONICAL_ROOT", str(tmp_path / "lake"))
    monkeypatch.setenv("DATACENTER_EVIDENCE_ROOT", str(tmp_path / "evidence"))
    # The legacy 1m unit passes no --symbols: its scope came from the machine env.
    monkeypatch.setenv("DATACENTER_MAINTENANCE_SYMBOLS", ",".join(LEGACY_SYMBOLS))

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


def test_unit_root_is_found_from_an_installed_release(tmp_path):
    """A release installs the package under .venv/lib, where parents[3] is not the root.

    The default used to land inside the virtualenv, so a production run reported
    no declared units and every host unit as host-only.
    """
    release = tmp_path / "releases" / "abc123"
    (release / "deploy" / "systemd").mkdir(parents=True)
    (release / "deploy" / "systemd" / "market-data-center-1m-maintenance.service").write_text(
        "[Service]\nExecStart=/x/.venv/bin/python -m data_center.maintenance_runner\n")
    installed = release / ".venv" / "lib" / "python3.11" / "site-packages" / "data_center"
    installed.mkdir(parents=True)
    module = installed / "takeover.py"
    module.write_text("")

    assert default_unit_root(module_file=module,
                             manifest=release / "deployment.json") == release / "deploy" / "systemd"
    # Without a manifest the search still finds the release by walking up.
    assert default_unit_root(module_file=module) == release / "deploy" / "systemd"
    # A tree with no unit files at all is reported as unknown, not as "none declared".
    empty = tmp_path / "elsewhere" / "data_center"
    empty.mkdir(parents=True)
    (empty / "takeover.py").write_text("")
    assert default_unit_root(module_file=empty / "takeover.py") is None


def test_a_wrapper_unit_is_recognised_with_an_unknown_scope(tmp_path):
    """The macro-market-lab entry runs a script: it is captured, never guessed."""
    script = tmp_path / "marketlab-maintain-market-bars.sh"
    script.write_text("#!/bin/sh\nexec python -m data_center.derived_maintenance_runner --start x\n")
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(f"""#!/bin/sh
if [ "$1" = "list-unit-files" ]; then
  echo "marketlab-market-bars-maintenance.service static"
  echo "market-data-center-unknown-producer.service static"
  echo "dbus.service static"
  exit 0
fi
if [ "$1" = "show" ]; then
  case "$2" in
    marketlab-market-bars-maintenance.service)
      case "$4" in
        Environment) echo "MARKETLAB_MARKET_BARS_BACKEND=data_center MARKETLAB_DATA_ROOT=/x" ;;
        ExecStart) echo "{{ path=/bin/sh ; argv[]=/bin/sh {script} ; }}" ;;
        *) echo "" ;;
      esac ;;
    marketlab-market-bars-maintenance.timer)
      echo "{{ OnCalendar=*-*-* 06:30:00 UTC }}" ;;
    *) echo "" ;;
  esac
  exit 0
fi
exit 0
""")
    systemctl.chmod(0o755)
    inventory = host_inventory(systemctl=(str(systemctl),))
    entries = entries_from_inventory(inventory)
    assert [entry.unit for entry in entries] == ["marketlab-market-bars-maintenance.service"]
    entry = entries[0]
    # The runner is a field and the command is the one that actually produces
    # data: no marker is smuggled into a command line that parsing reads back.
    assert entry.runner_module == "data_center.derived_maintenance_runner"
    assert entry.command == "python -m data_center.derived_maintenance_runner --start x"
    assert "#" not in entry.command and "wrapper" not in entry.command
    assert entry.calendar == "*-*-* 06:30:00 UTC"
    # The wrapper names no symbols (the machine allowlist is the scope it really
    # had) and no recipes: the cadence is readable from its timer, the outputs are
    # not, so the entry is captured yet not importable without the operator.
    assert legacy_symbols(entry, maintenance_symbols=LEGACY_SYMBOLS) == sorted(LEGACY_SYMBOLS)
    assert schedule_for(entry) == {"schedule": "daily", "timezone": "UTC", "local_time": "06:30"}
    comparison = compare_entries(planned_entries(UNIT_ROOT, inventory), inventory,
                                 declared_units=set(), maintenance_symbols=LEGACY_SYMBOLS)
    assert comparison["unmappable"] == ["marketlab-market-bars-maintenance.service"]
    # The captured wrapper is not "undeclared"; the report stays a governance
    # list, so a governed-looking unit shows up while a system daemon does not.
    assert comparison["installed_not_declared"] == ["market-data-center-unknown-producer.service"]


def test_declared_entry_is_not_reported_as_host_only(tmp_path):
    entry = planned_entries(UNIT_ROOT)[0]
    comparison = compare_entries([entry], {"status": "known", "units": [entry.unit]},
                                 declared_units={entry.unit}, maintenance_symbols=LEGACY_SYMBOLS)
    assert comparison["entries"][0]["host_only"] is False


def test_a_delay_entry_and_a_calendar_entry_merge_on_one_instrument():
    """Both cadences survive the merge and the faster one is imported."""
    delay = LegacyEntry(unit="raw.service", timer="raw.timer",
                        command="python -m data_center.maintenance_runner --provider dukascopy "
                                f"--symbols {' '.join(LEGACY_SYMBOLS)}",
                        argv=("python", "-m", "data_center.maintenance_runner", "--provider", "dukascopy",
                              "--symbols", *LEGACY_SYMBOLS),
                        cadence_seconds=900, cadence_source="OnUnitInactiveSec", requires_api=True)
    calendar = LegacyEntry(unit="derived.service", timer="derived.timer",
                           command="python -m data_center.derived_maintenance_runner --provider dukascopy "
                                   "--symbols EURUSD --recipes utc-24x7-1m-to-5m-ohlcv",
                           argv=("python", "-m", "data_center.derived_maintenance_runner",
                                 "--provider", "dukascopy", "--symbols", "EURUSD",
                                 "--recipes", "utc-24x7-1m-to-5m-ohlcv"),
                           cadence_seconds=None, cadence_source="OnCalendar", requires_api=False,
                           calendar="*-*-* 06:30:00 UTC")
    merged = merged_definitions([delay, calendar])
    eurusd = next(item for item in merged if item["symbol"] == "EURUSD")
    # The delay is the more frequent trigger, so it is the plan's schedule...
    assert eurusd["schedule"] == {"schedule": "fixed_delay", "interval_seconds": 900}
    # ...and the receipt still shows what each entry actually did.
    assert eurusd["cadences"] == {
        "raw.service": {"schedule": "fixed_delay", "interval_seconds": 900},
        "derived.service": {"schedule": "daily", "timezone": "UTC", "local_time": "06:30"},
    }
    assert eurusd["bar_timeframes"] == ["5m"] and eurusd["sources"] == ["raw.service", "derived.service"]
    assert schedule_seconds({"schedule": "daily"}) == 86400.0
    assert schedule_seconds({"schedule": "manual"}) == float("inf")


def test_an_entry_can_state_the_price_basis_its_provider_publishes():
    """One wrapper covers several providers; each publishes its own basis."""
    wrapper = bytes(json.dumps({"entries": [
        {"unit": "macro-binance", "exec_start": "python -m data_center.derived_maintenance_runner "
                                                 "--provider binance --symbols BTCUSDT "
                                                 "--recipes utc-24x7-1m-to-5m-ohlcv",
         "price_basis": "raw", "calendar": "*-*-* 06:30:00 UTC", "cadence_source": "OnCalendar"}]}),
        "utf-8").decode()
    entry = entries_from_inventory(json.loads(wrapper))[0]
    assert entry.price_basis == "raw"
    definition = plan_definitions(entry)[0]
    assert definition["price_basis"] == "raw" and definition["provider"] == "binance"
    # Without the statement the caller's default applies, and binance rejects it.
    plain = LegacyEntry(unit="macro-binance", timer=None,
                        command=entry.command, argv=entry.argv, cadence_seconds=None,
                        cadence_source="OnCalendar", requires_api=False,
                        calendar="*-*-* 06:30:00 UTC")
    assert plan_definitions(plain)[0]["price_basis"] == "bid"
