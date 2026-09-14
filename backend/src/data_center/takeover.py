"""Prepare — never perform — the production takeover of the legacy timers.

Spec 9 and plan S5.2 ask for a controlled handover: capture what the old entry
points actually do, import them as *paused* production plans, and prove in a
comparison receipt that the new plans do not widen the production scope.  Then,
and only then, an operator blocks the old entries and enables a canary.

Nothing in this module installs, stops or starts a unit, and nothing here writes
to a ledger unless the caller explicitly applies it.  The host's own systemd
state is the authority: :func:`declared_entries` reads what the repository ships,
and :func:`host_inventory` reads what the machine actually has, so a missing or
extra unit is reported instead of assumed.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .evidence import operation_receipt, write_receipt
from .platform_registry import REGISTRY
from .production_tasks import DEFAULT_RAW_TIMEFRAME, ProductionConflict, ProductionTasks

#: Runner modules that predate the scheduler and are therefore legacy entries.
LEGACY_RUNNER_MODULES = ("data_center.maintenance_runner", "data_center.derived_maintenance_runner")
#: The cadence the old units used, in seconds, when a timer says "15min".
_CADENCE_UNITS = {"s": 1, "sec": 1, "min": 60, "h": 3600, "d": 86400}


@dataclass(frozen=True)
class LegacyEntry:
    """One legacy entry point, as declared by the repository or read from a host."""

    unit: str
    timer: str | None
    command: str
    argv: tuple[str, ...]
    cadence_seconds: int | None
    cadence_source: str | None
    requires_api: bool

    def as_dict(self) -> dict:
        return {"unit": self.unit, "timer": self.timer, "command": self.command,
                "argv": list(self.argv), "cadence_seconds": self.cadence_seconds,
                "cadence_source": self.cadence_source, "requires_api": self.requires_api}


def _unit_value(text: str, key: str) -> str | None:
    match = re.search(rf"^{key}=(.*)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _duration_seconds(value: str) -> int | None:
    match = re.fullmatch(r"(\d+)\s*([a-zA-Z]*)", value.strip())
    if match is None:
        return None
    unit = (match.group(2) or "s").lower()
    return int(match.group(1)) * _CADENCE_UNITS.get(unit, 1)


def declared_entries(unit_root: Path) -> list[LegacyEntry]:
    """Legacy entries the repository ships, read from the unit files themselves."""
    entries: list[LegacyEntry] = []
    for service_path in sorted(Path(unit_root).glob("*.service")):
        text = service_path.read_text()
        command = _unit_value(text, "ExecStart") or ""
        if not any(module in command for module in LEGACY_RUNNER_MODULES):
            continue
        timer_path = service_path.with_suffix(".timer")
        timer_text = timer_path.read_text() if timer_path.is_file() else ""
        cadence, source = None, None
        if timer_text:
            inactive = _unit_value(timer_text, "OnUnitInactiveSec")
            calendar = _unit_value(timer_text, "OnCalendar")
            boot = _unit_value(timer_text, "OnBootSec")
            if inactive:
                cadence, source = _duration_seconds(inactive), "OnUnitInactiveSec"
            elif calendar:
                cadence, source = None, "OnCalendar"
            elif boot:
                cadence, source = _duration_seconds(boot), "OnBootSec"
        entries.append(LegacyEntry(
            unit=service_path.name,
            timer=timer_path.name if timer_path.is_file() else None,
            command=command,
            argv=tuple(shlex.split(command)),
            cadence_seconds=cadence,
            cadence_source=source,
            requires_api="market-data-center-api.service" in text,
        ))
    return entries


def host_inventory(*, systemctl: tuple[str, ...] = ("systemctl", "--user")) -> dict:
    """What the host actually has installed; a host that cannot be read says so."""
    try:
        result = subprocess.run([*systemctl, "list-unit-files", "--type=service", "--type=timer",
                                 "--no-legend", "--plain"], capture_output=True, text=True,
                                check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "unknown", "error": type(exc).__name__, "units": []}
    if result.returncode != 0:
        return {"status": "unknown", "error": f"exit {result.returncode}", "units": []}
    units = sorted({line.split()[0] for line in result.stdout.splitlines() if line.strip()})
    return {"status": "known", "units": units}


def entries_from_inventory(inventory: dict | None) -> list[LegacyEntry]:
    """Legacy entries that only exist on the host.

    The host is authoritative (spec 9): a unit installed from an older branch may
    not be declared in this repository at all, and it still has to be captured,
    imported as a paused plan and blocked before the new scheduler takes over.
    """
    entries: list[LegacyEntry] = []
    for item in (inventory or {}).get("entries") or []:
        command = str(item.get("exec_start") or item.get("command") or "")
        if not any(module in command for module in LEGACY_RUNNER_MODULES):
            continue
        cadence = item.get("cadence_seconds")
        entries.append(LegacyEntry(
            unit=str(item.get("unit") or "unknown.service"),
            timer=item.get("timer"),
            command=command,
            argv=tuple(shlex.split(command)),
            cadence_seconds=int(cadence) if cadence is not None else None,
            cadence_source=item.get("cadence_source") or ("host_inventory" if cadence else None),
            requires_api=bool(item.get("requires_api", False)),
        ))
    return entries


def planned_entries(unit_root: Path, inventory: dict | None = None) -> list[LegacyEntry]:
    """Declared entries plus host-only ones, with the repository's file preferred."""
    entries = declared_entries(unit_root)
    seen = {entry.unit for entry in entries}
    return entries + [entry for entry in entries_from_inventory(inventory) if entry.unit not in seen]


def compare_entries(entries: list[LegacyEntry], inventory: dict | None = None, *,
                    declared_units: set[str] | None = None) -> dict:
    """Old vs new: cadence, scope and unit presence, before anything is imported."""
    rows = []
    installed = set((inventory or {}).get("units") or [])
    for entry in entries:
        argv = entry.argv
        provider = _argument(argv, "--provider") or "dukascopy"
        symbols = _arguments(argv, "--symbols") or [item.symbol for item in
                                                     REGISTRY.instruments(provider)]
        recipes = _arguments(argv, "--recipes")
        rows.append({
            "unit": entry.unit,
            "timer": entry.timer,
            "runner": next((module for module in LEGACY_RUNNER_MODULES if module in entry.command), None),
            "provider": provider,
            "symbols": sorted(symbols),
            "recipes": sorted(recipes),
            "raw_timeframe": DEFAULT_RAW_TIMEFRAME,
            "cadence_seconds": entry.cadence_seconds,
            "cadence_source": entry.cadence_source,
            "installed": None if (inventory or {}).get("status") != "known"
            else all(name in installed for name in (entry.unit, entry.timer) if name),
            # A unit that only exists on the host still has to be captured: the
            # repository is not the authority on what the machine runs.
            "host_only": None if declared_units is None else entry.unit not in declared_units,
        })
    return {"entries": rows,
            "host_status": (inventory or {}).get("status", "not_checked"),
            "declared_units": sorted(declared_units) if declared_units is not None else None,
            "installed_not_declared": sorted(installed - {name for entry in entries
                                                          for name in (entry.unit, entry.timer) if name})
            if (inventory or {}).get("status") == "known" else []}


def _derivable_targets(*, provider: str, price_basis: str, recipes: list[str]) -> list[str]:
    """Target timeframes this provider can actually derive, in recipe order."""
    from .production_tasks import DefinitionError, _recipe_chain

    targets = []
    for recipe in REGISTRY.recipes():
        if recipes and recipe.recipe_id not in recipes:
            continue
        try:
            chain = _recipe_chain(recipe.target_timeframe, raw_timeframe=DEFAULT_RAW_TIMEFRAME,
                                  provider=provider, price_basis=price_basis)
        except DefinitionError:
            chain = None
        if chain and recipe.target_timeframe not in targets:
            targets.append(recipe.target_timeframe)
    return sorted(targets)


def _argument(argv: tuple[str, ...], flag: str) -> str | None:
    values = _arguments(argv, flag)
    return values[0] if values else None


def _arguments(argv: tuple[str, ...], flag: str) -> list[str]:
    if flag not in argv:
        return []
    index = argv.index(flag)
    values = []
    for value in argv[index + 1:]:
        if value.startswith("--"):
            break
        values.append(value)
    return values


def plan_definitions(entry: LegacyEntry, *, price_basis: str = "bid") -> list[dict]:
    """The definitions one legacy entry maps to, one plan per instrument.

    The scope is derived from the entry's own arguments: a raw maintenance entry
    imports raw plans only, and a derived entry imports the recipes it names.
    Nothing is widened beyond what the unit already asked for.
    """
    argv = entry.argv
    provider = _argument(argv, "--provider") or "dukascopy"
    symbols = _arguments(argv, "--symbols") or [item.symbol for item in REGISTRY.instruments(provider)]
    recipes = _arguments(argv, "--recipes")
    raw_only = "data_center.derived_maintenance_runner" not in entry.command
    definitions = []
    for symbol in sorted(symbols):
        definitions.append({
            "provider": provider, "symbol": symbol,
            "raw_timeframe": DEFAULT_RAW_TIMEFRAME, "price_basis": price_basis,
            # Only targets whose chain actually resolves for this provider: an
            # imported plan must not promise a derivation the registry cannot do.
            "bar_timeframes": [] if raw_only else _derivable_targets(
                provider=provider, price_basis=price_basis, recipes=recipes),
            "window_policy": {"mode": "continuous",
                              "history_start": _history_start(provider, DEFAULT_RAW_TIMEFRAME).isoformat()},
            # The old timer fired 15 minutes *after* the previous run finished:
            # that is exactly fixed_delay, and never a fixed clock time.
            "schedule": {"schedule": "fixed_delay",
                         "interval_seconds": entry.cadence_seconds or 900},
        })
    return definitions


def _history_start(provider: str, raw_timeframe: str) -> datetime:
    """The old runner's own tail window, expressed as the plan's history start.

    The legacy runner plans from its registered tail (two days for Dukascopy), so
    the imported plan starts where the old entry effectively started instead of
    asking for history the previous entry never fetched.
    """
    from .platform_registry import maintenance_policy_for

    policy = maintenance_policy_for(provider, raw_timeframe)
    return (datetime.now(timezone.utc) - timedelta(days=max(1, policy.tail_days))).replace(microsecond=0)


def merged_definitions(entries: list[LegacyEntry], *, price_basis: str = "bid") -> list[dict]:
    """One plan per provider/symbol, covering what *all* its legacy entries produced.

    A raw entry and a derived entry for the same instrument are two halves of one
    ownership scope in the new model, so they import as a single plan: two plans
    would conflict on the raw ownership key, and the derived outputs would have no
    owner.  The more frequent cadence wins, because a slower schedule would stop
    producing what the faster entry used to produce, and the difference is
    reported in the comparison receipt for the operator to accept.
    """
    grouped: dict[tuple[str, str], dict] = {}
    for entry in entries:
        for definition in plan_definitions(entry, price_basis=price_basis):
            key = (definition["provider"], definition["symbol"])
            current = grouped.get(key)
            cadence = definition["schedule"]["interval_seconds"]
            if current is None:
                grouped[key] = {**definition, "sources": [entry.unit],
                                "cadences": {entry.unit: cadence}}
                continue
            current["bar_timeframes"] = sorted(set(current["bar_timeframes"])
                                               | set(definition["bar_timeframes"]))
            current["sources"].append(entry.unit)
            current["cadences"][entry.unit] = cadence
            if cadence < current["schedule"]["interval_seconds"]:
                current["schedule"] = {**definition["schedule"]}
    return [grouped[key] for key in sorted(grouped)]


def import_entries(service: ProductionTasks, *, entries: list[LegacyEntry], actor: str,
                   apply: bool = False, price_basis: str = "bid",
                   now: datetime | None = None) -> dict:
    """Import legacy entries as paused plans; without ``apply`` nothing is written."""
    moment = now or datetime.now(timezone.utc)
    import_rows, created, skipped = [], [], []
    existing = {task["task_id"] for task in service.ledger.list_production_tasks(include_deleted=True)}
    for definition in merged_definitions(entries, price_basis=price_basis):
        task_id = f"legacy-{definition['provider']}-{definition['symbol']}-{definition['raw_timeframe']}"
        row = {"task_id": task_id, "symbol": definition["symbol"],
               "sources": definition["sources"], "cadences": definition["cadences"],
               "bar_timeframes": definition["bar_timeframes"],
               "schedule": definition["schedule"]["schedule"],
               "interval_seconds": definition["schedule"]["interval_seconds"]}
        import_rows.append(row)
        if task_id in existing:
            skipped.append({**row, "reason": "already_imported"})
            continue
        if not apply:
            skipped.append({**row, "reason": "dry_run"})
            continue
        payload = {key: value for key, value in definition.items()
                   if key not in {"sources", "cadences"}}
        try:
            # Imported plans are paused by construction: enabling production is a
            # separate, explicit decision (plan S5.2 step 4).
            plan = service.create(definition=payload, name=task_id, task_id=task_id,
                                  desired_state="paused", actor=actor, now=moment)
        except ProductionConflict as exc:
            skipped.append({**row, "reason": exc.code})
            continue
        created.append({**row, "definition_version": plan["definition_version"]})
    return {"apply": apply, "created": created, "skipped": skipped,
            "rows": import_rows, "at": moment.isoformat()}


def verify_takeover(service: ProductionTasks, *, entries: list[LegacyEntry],
                    inventory: dict | None = None) -> dict:
    """Post-conditions an operator must be able to show after a handover."""
    problems: list[str] = []
    rows = []
    for definition in merged_definitions(entries):
        task_id = f"legacy-{definition['provider']}-{definition['symbol']}-{definition['raw_timeframe']}"
        task = service.ledger.get_production_task(task_id)
        if task is None:
            problems.append(f"{task_id} was never imported")
            continue
        document = service.read(task_id)
        expected = definition["schedule"]["interval_seconds"]
        actual = (task["payload"].get("schedule") or {}).get("interval_seconds")
        if actual != expected:
            problems.append(f"{task_id} cadence {actual} does not match the legacy {expected}")
        rows.append({"task_id": task_id, "desired_state": task["desired_state"],
                     "health": document["health"], "block_reason": document["block_reason"],
                     "interval_seconds": actual})
    # Two writers for one output is the failure this whole handover exists to avoid.
    for task in service.ledger.list_production_tasks():
        if task["desired_state"] != "enabled":
            continue
        keys = [item["ownership_key"] for item in service.ledger.ownership_of(task["task_id"])]
        for holder in service.ledger.ownership_holders(keys):
            if holder.get("task_id") not in (None, task["task_id"]):
                problems.append(
                    f"{holder['ownership_key']} is held by {holder['task_id']} and {task['task_id']}")
    comparison = compare_entries(entries, inventory)
    if comparison["host_status"] == "known":
        still_installed = [row["unit"] for row in comparison["entries"] if row["installed"]]
        if still_installed:
            problems.append(f"legacy units are still installed: {', '.join(sorted(still_installed))}")
    return {"problems": problems, "plans": rows, "comparison": comparison}


def _receipt(evidence_root: Path | None, *, action: str, result: str, details: dict,
             started_at: str) -> str | None:
    path = write_receipt(evidence_root, operation_receipt(
        action=action, command=f"data_center.takeover {action.split('.')[-1]}",
        started_at=started_at, result=result, details=details))
    return None if path is None else str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare the legacy timer takeover (never performs it)")
    parser.add_argument("command", choices=("plan", "import", "verify"))
    parser.add_argument("--unit-root", default=None, help="defaults to <repo>/deploy/systemd")
    parser.add_argument("--inventory", default=None,
                        help="a JSON file with the host's unit list; omit to read systemctl")
    parser.add_argument("--price-basis", default="bid")
    parser.add_argument("--apply", action="store_true",
                        help="import actually writes paused plans; without it nothing changes")
    parser.add_argument("--actor", default="operator:takeover")
    parser.add_argument("--evidence-root", default=None)
    args = parser.parse_args(argv)

    from .settings import Settings

    settings = Settings()
    unit_root = Path(args.unit_root) if args.unit_root else Path(__file__).resolve().parents[3] / "deploy" / "systemd"
    inventory = json.loads(Path(args.inventory).read_text()) if args.inventory else host_inventory()
    entries = planned_entries(unit_root, inventory)
    started = datetime.now(timezone.utc).isoformat()
    evidence_root = Path(args.evidence_root) if args.evidence_root else settings.evidence_root

    declared_units = {entry.unit for entry in declared_entries(unit_root)}
    if args.command == "plan":
        details = compare_entries(entries, inventory, declared_units=declared_units)
        print(json.dumps({"event": "takeover_plan", **details}, indent=2, sort_keys=True))
        return 0

    from .runs.ledger import RunLedger

    service = ProductionTasks(RunLedger(settings.ledger_path), canonical_root=settings.canonical_root)
    if args.command == "import":
        result = import_entries(service, entries=entries, actor=args.actor, apply=args.apply,
                                price_basis=args.price_basis)
        result["comparison"] = compare_entries(entries, inventory)
        path = _receipt(evidence_root, action="production_takeover_import",
                        result="pass", details=result, started_at=started)
        print(json.dumps({"event": "takeover_import", "applied": args.apply,
                          "created": len(result["created"]), "skipped": len(result["skipped"]),
                          "receipt": path}, indent=2, sort_keys=True))
        return 0

    report = verify_takeover(service, entries=entries, inventory=inventory)
    path = _receipt(evidence_root, action="production_takeover_verify",
                    result="failed" if report["problems"] else "pass",
                    details=report, started_at=started)
    print(json.dumps({"event": "takeover_verify", "problems": report["problems"],
                      "receipt": path}, indent=2, sort_keys=True))
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
