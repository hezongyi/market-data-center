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
from .operations_views import GOVERNANCE_UNITS
from .platform_registry import REGISTRY
from .production_tasks import (
    DEFAULT_RAW_TIMEFRAME,
    DefinitionError,
    ProductionConflict,
    ProductionTasks,
)

#: Runner modules that predate the scheduler and are therefore legacy entries.
LEGACY_RUNNER_MODULES = ("data_center.maintenance_runner", "data_center.derived_maintenance_runner")

#: The runner that produces *derived* outputs.  An entry that reaches it owns
#: recipes, not just raw windows, so the distinction decides the imported scope.
DERIVED_RUNNER_MODULE = "data_center.derived_maintenance_runner"
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
    #: A calendar expression when the timer is not a monotonic delay, kept as the
    #: host spelled it instead of being flattened into a made-up interval.
    calendar: str | None = None
    #: The price basis this entry produced.  One wrapper can carry several
    #: instruments whose provider publishes a different basis (binance publishes
    #: ``raw`` where dukascopy publishes ``bid``), so the operator states it.
    price_basis: str | None = None
    #: The runner module this entry reaches, when the host knows it.  It decides
    #: whether the entry owns derived outputs, so it is carried as a field instead
    #: of being smuggled into the command string for a later re-parse.
    runner_module: str | None = None

    def as_dict(self) -> dict:
        return {"unit": self.unit, "timer": self.timer, "command": self.command,
                "argv": list(self.argv), "cadence_seconds": self.cadence_seconds,
                "cadence_source": self.cadence_source, "requires_api": self.requires_api,
                "calendar": self.calendar, "price_basis": self.price_basis,
                "runner_module": self.runner_module}


def _unit_value(text: str, key: str) -> str | None:
    match = re.search(rf"^{key}=(.*)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _duration_seconds(value: str) -> int | None:
    match = re.fullmatch(r"(\d+)\s*([a-zA-Z]*)", value.strip())
    if match is None:
        return None
    unit = (match.group(2) or "s").lower()
    return int(match.group(1)) * _CADENCE_UNITS.get(unit, 1)


def default_unit_root(*, module_file: Path | None = None,
                      manifest: Path | None = None) -> Path | None:
    """Where the repository's unit files are, from a checkout *or* a release.

    ``Path(__file__).parents[3]`` only works while the module lives in a source
    tree; a release installs the package under ``.venv/lib/...`` where that
    arithmetic lands inside the virtualenv and silently reports "no declared
    units", turning every host unit into a host-only one.  The deployment
    manifest names the release root, so it is consulted as well.
    """
    candidates = []
    source = Path(module_file or __file__).resolve()
    # Walk every parent instead of a fixed depth: a release installs the package
    # deeper than a checkout, and a depth tuned to one layout silently reports
    # "no declared units" for the other.
    for parent in source.parents:
        candidates.append(parent / "deploy" / "systemd")
    if manifest is not None:
        candidates.insert(0, Path(manifest).resolve().parent / "deploy" / "systemd")
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.glob("*.service")):
            return candidate
    return None


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


def _systemd_argv(value: str) -> tuple[str, ...]:
    """Extract ``argv[]`` from ``systemctl show -p ExecStart`` output."""
    match = re.search(r"argv\[\]=(.*?)(?:\s*;\s*|\}$)", value)
    if match is None:
        return ()
    return tuple(shlex.split(match.group(1).strip()))


def _systemd_duration(value: str) -> int | None:
    """systemd prints either ``15min`` or a bare microsecond count."""
    text = value.strip()
    if text.isdigit():
        return int(text) // 1_000_000
    return _duration_seconds(text)


def host_inventory(*, systemctl: tuple[str, ...] = ("systemctl", "--user"),
                   read_definitions: bool = True) -> dict:
    """What the host actually has installed *and* what those units execute.

    Names alone cannot be imported: a unit installed from an older branch needs its
    own ``ExecStart`` and cadence, so the inventory reads them with ``systemctl
    show``.  A property the host cannot report stays absent — never invented.
    """
    try:
        result = subprocess.run([*systemctl, "list-unit-files", "--type=service", "--type=timer",
                                 "--no-legend", "--plain"], capture_output=True, text=True,
                                check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "unknown", "error": type(exc).__name__, "units": [], "entries": []}
    if result.returncode != 0:
        return {"status": "unknown", "error": f"exit {result.returncode}", "units": [], "entries": []}
    units = sorted({line.split()[0] for line in result.stdout.splitlines() if line.strip()})
    entries = []
    if read_definitions:
        for unit in units:
            if not unit.endswith(".service"):
                continue
            entries.extend(_host_entry(unit, systemctl=systemctl))
    return {"status": "known", "units": units, "entries": entries}


#: Scripts that wrap the Data Center runners (the macro-market-lab entry).  The
#: hints are the module names' last segment, so they cannot drift from the list
#: of modules that actually are legacy runners.
RUNNER_SCRIPT_HINTS = tuple(module.rsplit(".", 1)[-1] for module in LEGACY_RUNNER_MODULES)


def _script_paths(command: str) -> list[Path]:
    """Existing shell or Python files a command line names, in order."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return []
    paths = []
    for token in argv:
        if token.startswith("-"):
            continue
        path = Path(token)
        if path.suffix in {".sh", ".bash", ".py"} and path.is_file():
            paths.append(path)
    return paths


def _shell_defaults(body: str) -> dict[str, str]:
    """Literal values a wrapper assigns to its own variables, best effort.

    A wrapper writes ``REPO_ROOT="${MARKETLAB_REPO_ROOT:-/home/quant/repos/...}"``
    and then hands control to ``${REPO_ROOT}/scripts/...``.  Only the literal or
    the ``:-default`` half of such an assignment is read: an unresolved variable
    simply means the next level cannot be followed, which is reported by the
    caller instead of guessed.
    """
    values: dict[str, str] = {}
    for line in body.splitlines():
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not match:
            continue
        name, raw = match.group(1), match.group(2).strip().rstrip(";")
        default = re.fullmatch(r"[\"']?\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}[\"']?", raw)
        literal = re.fullmatch(r"[\"']([^\"'$]*)[\"']", raw)
        if default and "$(" not in default.group(1):
            # A command substitution is not a literal, so it stays unresolved.
            values[name] = default.group(1)
        elif literal:
            values[name] = literal.group(1)
    return values


def _expand(text: str, values: dict[str, str]) -> str:
    """Substitute the wrappers' own literal variables; leave the rest untouched."""
    def replace(match: re.Match) -> str:
        return values.get(match.group(1) or match.group(2), match.group(0))

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", replace, text)


def _runner_command(body: str) -> str | None:
    """The command line inside a script that runs a Data Center runner.

    Backslash continuations are joined so the runner's own arguments (provider,
    symbols, recipes) stay readable instead of being lost to line wrapping.
    """
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if not any(hint in line for hint in RUNNER_SCRIPT_HINTS):
            continue
        command = line.strip()
        while command.endswith("\\") and index + 1 < len(lines):
            index += 1
            command = f"{command[:-1].rstrip()} {lines[index].strip()}"
        return re.sub(r"^exec\s+", "", command)
    return None


def _script_command(exec_start: str, *, depth: int = 3) -> tuple[str, str | None]:
    """Follow a wrapper chain to the command that reaches a Data Center runner.

    The macro-market-lab entry goes through two scripts before the runner appears
    (the unit runs ``marketlab-maintain-market-bars.sh``, which execs
    ``marketlab-maintain-market-bars-data-center.sh``).  Reading one level misses
    the runner *and* its arguments, which left the operator to supply the derived
    scope from outside the tool.  Returns the runner's own command line and the
    module it names; an unresolved level returns nothing rather than a guess.
    """
    if depth <= 0:
        return "", None
    for path in _script_paths(exec_start):
        try:
            body = path.read_text()
        except OSError:
            continue
        command = _runner_command(body)
        if command is not None:
            module = next((item for item in LEGACY_RUNNER_MODULES if item in command), None)
            return _expand(command, _shell_defaults(body)), module
        values = _shell_defaults(body)
        for line in body.splitlines():
            nested = re.match(r"\s*(?:exec|source|\.)\s+(.+)$", line)
            if nested is None:
                continue
            command, module = _script_command(_expand(nested.group(1), values), depth=depth - 1)
            if command:
                return command, module
    return "", None


#: A wrapper that points the other repo at the Data Center backend produces data
#: through this platform's runners, so it belongs in the takeover inventory.  The
#: marker names *derived* maintenance explicitly, because that is the entry the
#: integration routes to this platform (`docs/integration/macro-market-lab.md`).
DATA_CENTER_BACKEND_MARKERS = ("MARKETLAB_MARKET_BARS_BACKEND=data_center",)
DATA_CENTER_BACKEND_RUNNER = "data_center.derived_maintenance_runner"


def _host_entry(unit: str, *, systemctl: tuple[str, ...]) -> list[dict]:
    """One host unit as an importable entry, or nothing when it is not a runner."""

    def show(*properties: str, target: str | None = None) -> str:
        try:
            result = subprocess.run([*systemctl, "show", target or unit, *properties],
                                    capture_output=True, text=True, check=False, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout if result.returncode == 0 else ""

    text = show("-p", "ExecStart", "--value")
    argv = _systemd_argv(text)
    command = " ".join(argv)
    # The macro-market-lab wrapper selects this platform's backend through its
    # unit environment, not through its command line, so that is read too.
    environment = show("-p", "Environment", "--value")
    module = next((item for item in LEGACY_RUNNER_MODULES if item in command), None)
    if module is None:
        followed, module = _script_command(command)
        if module is None:
            if not any(marker in command or marker in followed or marker in environment
                       for marker in DATA_CENTER_BACKEND_MARKERS):
                return []
            # The wrapper names no runner but says it uses this platform's
            # backend: that integration is the derived maintenance entry.
            module = DATA_CENTER_BACKEND_RUNNER
        if followed:
            # Report the command that actually produces data, so the runner's own
            # arguments (provider, symbols, recipes) are readable.  The runner
            # itself is a field, never a comment smuggled into the command where
            # argument parsing would read it back.
            command = followed
    timer_unit = unit[: -len(".service")] + ".timer"
    timer_properties = show("-p", "TimersMonotonic", "-p", "TimersCalendar", "--value",
                            target=timer_unit)
    cadence, calendar = None, None
    match = re.search(r"OnUnitInactiveUSec=(\S+?)[,}\s]", timer_properties + " ")
    if match:
        cadence = _systemd_duration(match.group(1))
    # systemd prints the calendar plus a "next_elapse" hint: keep the calendar.
    match = re.search(r"OnCalendar=([^,;}]+)", timer_properties)
    if match:
        calendar = match.group(1).strip()
    requires_api = "market-data-center-api.service" in show("-p", "Requires", "--value")
    return [{"unit": unit,
             "timer": timer_unit if "Timers" in timer_properties else None,
             "exec_start": command, "runner_module": module, "cadence_seconds": cadence,
             "cadence_source": "OnUnitInactiveSec" if cadence else ("OnCalendar" if calendar else None),
             "calendar": calendar, "requires_api": requires_api}]


def entries_from_inventory(inventory: dict | None) -> list[LegacyEntry]:
    """Legacy entries that only exist on the host.

    The host is authoritative (spec 9): a unit installed from an older branch may
    not be declared in this repository at all, and it still has to be captured,
    imported as a paused plan and blocked before the new scheduler takes over.
    """
    entries: list[LegacyEntry] = []
    for item in (inventory or {}).get("entries") or []:
        command = str(item.get("exec_start") or item.get("command") or "")
        module = str(item.get("runner_module") or "") or next(
            (name for name in LEGACY_RUNNER_MODULES if name in command), "")
        if not module:
            continue
        cadence = item.get("cadence_seconds")
        calendar = item.get("calendar")
        entries.append(LegacyEntry(
            unit=str(item.get("unit") or "unknown.service"),
            timer=item.get("timer"),
            command=command,
            argv=tuple(shlex.split(command)),
            cadence_seconds=int(cadence) if cadence is not None else None,
            cadence_source=item.get("cadence_source") or ("host_inventory" if cadence else None),
            requires_api=bool(item.get("requires_api", False)),
            calendar=str(calendar) if calendar else None,
            price_basis=str(item["price_basis"]) if item.get("price_basis") else None,
            runner_module=module,
        ))
    return entries


def planned_entries(unit_root: Path, inventory: dict | None = None) -> list[LegacyEntry]:
    """Declared entries plus host-only ones, with the repository's file preferred."""
    entries = declared_entries(unit_root)
    seen = {entry.unit for entry in entries}
    return entries + [entry for entry in entries_from_inventory(inventory) if entry.unit not in seen]


def declared_unit_files(unit_root: Path) -> set[str]:
    """Every unit file the repository ships, runner or not (spec 3.4).

    ``installed_not_declared`` is a governance report about the repository's own
    unit files, so it has to compare against all of them: a declared API, worker
    or monitor unit is declared, even though it is not a legacy runner entry.
    """
    return {path.name for pattern in ("*.service", "*.timer")
            for path in Path(unit_root).glob(pattern)}


def compare_entries(entries: list[LegacyEntry], inventory: dict | None = None, *,
                    declared_units: set[str] | None = None,
                    declared_unit_files: set[str] | None = None,
                    maintenance_symbols: tuple[str, ...] = (),
                    price_basis: str = "bid") -> dict:
    """Old vs new: cadence, scope and unit presence, before anything is imported."""
    rows = []
    installed = set((inventory or {}).get("units") or [])
    for entry in entries:
        argv = entry.argv
        provider = _argument(argv, "--provider") or "dukascopy"
        symbols = legacy_symbols(entry, maintenance_symbols=maintenance_symbols) or []
        scope = provider_symbols(provider, symbols)
        recipe_ids, recipe_versions = _recipe_arguments(argv)
        recipes = sorted(f"{identifier}@{version}" for identifier in recipe_ids
                         for version in (recipe_versions or {""}))
        rows.append({
            "unit": entry.unit,
            "timer": entry.timer,
            "provider": provider,
            "symbols": sorted(symbols),
            # The allowlist is provider-agnostic, so the entry is reported with
            # both what this provider will produce and what it will not: dropping
            # the difference silently is how a narrowed scope becomes invisible.
            **scope.as_dict(),
            "recipes": sorted(recipes),
            "raw_timeframe": DEFAULT_RAW_TIMEFRAME,
            "cadence_seconds": entry.cadence_seconds,
            "cadence_source": entry.cadence_source,
            "calendar": entry.calendar,
            "calendar_zone_assumed": bool(entry.calendar) and not _calendar_zone(entry.calendar),
            "runner": entry.runner_module or next(
                (module for module in LEGACY_RUNNER_MODULES if module in entry.command), None),
            "derived": not raw_only(entry),
            # One predicate, shared with the import: a unit is mappable exactly
            # when a definition can be built for it.
            "mappable": bool(plan_definitions(entry, price_basis=price_basis,
                                              maintenance_symbols=maintenance_symbols)),
            "scope_source": ("argv" if _arguments(argv, "--symbols")
                             else "DATACENTER_MAINTENANCE_SYMBOLS" if maintenance_symbols else None),
            "installed": None if (inventory or {}).get("status") != "known"
            else all(name in installed for name in (entry.unit, entry.timer) if name),
            # A unit that only exists on the host still has to be captured: the
            # repository is not the authority on what the machine runs.
            "host_only": None if declared_units is None else entry.unit not in declared_units,
        })
    governed_prefixes = ("market-data-center", "marketlab")
    # Governance timers are known platform units even when this repository does
    # not ship them, so they are not an undeclared finding (spec 3.4).
    known = {name for name, _kind in GOVERNANCE_UNITS}
    known |= declared_unit_files or set()
    known |= {name for entry in entries for name in (entry.unit, entry.timer) if name}
    undeclared = sorted(name for name in installed - known
                        if name.startswith(governed_prefixes))
    unmappable = [row["unit"] for row in rows if row["mappable"] is False]
    return {"entries": rows,
            "unmappable": unmappable,
            # Reported separately from "unmappable": the entry imports, but not
            # everything its own allowlist named.
            "partially_served": [row["unit"] for row in rows
                                 if row["unserved_symbols"] or row["unapproved_symbols"]],
            "host_status": (inventory or {}).get("status", "not_checked"),
            "declared_units": sorted(declared_units) if declared_units is not None else None,
            # Only units this platform could own: the raw systemd list is not a
            # governance finding, and burying the real gap in it hides it.
            "installed_not_declared": undeclared
            if (inventory or {}).get("status") == "known" else []}


def _calendar_zone(calendar: str) -> str | None:
    """The zone a systemd calendar states, if it states one at all."""
    match = re.fullmatch(
        r"\*-\*-\*\s+(\d{2}):(\d{2})(?::\d{2})?(?:\s+([A-Za-z_/]+))?", calendar.strip())
    return match.group(3) if match else None


def _recipe_arguments(argv: tuple[str, ...]) -> tuple[set[str], set[str]]:
    """Recipe ids and versions named by ``--recipes``, which may carry ``@version``.

    The macro-market-lab unit passes ``--recipes utc-24x7-1m-to-5m-ohlcv@1`` while
    the registry keys recipes by id and version separately, so both spellings have
    to match or the imported plan silently drops the output the entry produced.
    """
    ids: set[str] = set()
    versions: set[str] = set()
    for value in _arguments(argv, "--recipes"):
        for item in value.replace(",", " ").split():
            identifier, _, version = item.partition("@")
            if identifier:
                ids.add(identifier)
            if version:
                versions.add(version)
    return ids, versions


def _derivable_targets(*, provider: str, price_basis: str, recipes: list[str]) -> list[str]:
    """Target timeframes this provider can actually derive, in recipe order."""
    from .production_tasks import DefinitionError, _recipe_chain

    named = {item.partition("@")[0] for item in recipes}
    targets = []
    for recipe in REGISTRY.recipes():
        if named and recipe.recipe_id not in named:
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


@dataclass(frozen=True)
class SymbolScope:
    """One legacy allowlist, split by what this provider can actually produce.

    The machine allowlist is provider-agnostic (it lists every maintained
    instrument), so a dukascopy entry legitimately carries a symbol only binance
    serves.  Narrowing that silently would import a scope nobody asked for and
    destroy the evidence for it, so every part is kept and reported.
    """

    served: tuple[str, ...] = ()
    unapproved: tuple[str, ...] = ()
    unserved: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"importable_symbols": list(self.served),
                "unapproved_symbols": list(self.unapproved),
                "unserved_symbols": list(self.unserved)}


def provider_symbols(provider: str, symbols: list[str]) -> SymbolScope:
    """Split a legacy allowlist by what this provider publishes and approves.

    ``instruments`` is read unapproved as well: "this provider does not serve the
    symbol" and "the registry has it but has not approved it" are different
    findings, and only the first one means the allowlist named another provider.
    """
    instruments = {item.symbol: item for item in REGISTRY.instruments(provider, approved_only=False)}
    served, unapproved, unserved = [], [], []
    for symbol in symbols:
        instrument = instruments.get(symbol)
        if instrument is None:
            unserved.append(symbol)
        elif instrument.approved:
            served.append(symbol)
        else:
            unapproved.append(symbol)
    return SymbolScope(tuple(served), tuple(unapproved), tuple(unserved))


def legacy_symbols(entry: LegacyEntry, *, maintenance_symbols: tuple[str, ...] = ()) -> list[str] | None:
    """The instruments one legacy entry really covered.

    ``--symbols`` when the unit passes it, otherwise the machine-level
    ``DATACENTER_MAINTENANCE_SYMBOLS`` allowlist the runner itself fell back to.
    ``None`` means the scope cannot be established: claiming "all approved
    instruments" here would turn the no-widening proof into a tautology.
    """
    explicit = _arguments(entry.argv, "--symbols")
    if explicit:
        return sorted(set(explicit))
    if maintenance_symbols:
        return sorted(set(maintenance_symbols))
    return None


def schedule_for(entry: LegacyEntry) -> dict | None:
    """The new schedule that preserves the old trigger's meaning.

    A monotonic timer is a delay after the previous run (``fixed_delay``).  A
    calendar timer is a wall-clock time (``daily``).  Anything more complex is
    reported instead of being flattened into an interval nobody configured.
    """
    if entry.cadence_seconds:
        return {"schedule": "fixed_delay", "interval_seconds": int(entry.cadence_seconds)}
    if entry.calendar:
        # A systemd calendar may carry its zone ("*-*-* 06:30:00 UTC"); that is a
        # wall-clock time, which the new model expresses as a daily schedule.
        match = re.fullmatch(
            r"\*-\*-\*\s+(\d{2}):(\d{2})(?::\d{2})?(?:\s+([A-Za-z_/]+))?",
            entry.calendar.strip())
        if match:
            return {"schedule": "daily", "timezone": match.group(3) or "UTC",
                    "local_time": f"{match.group(1)}:{match.group(2)}"}
    return None


def plan_definitions(entry: LegacyEntry, *, price_basis: str = "bid",
                     maintenance_symbols: tuple[str, ...] = (),
                     now: datetime | None = None) -> list[dict]:
    """The definitions one legacy entry maps to, one plan per instrument.

    The scope is derived from the entry's own arguments: a raw maintenance entry
    imports raw plans only, and a derived entry imports the recipes it names.
    Nothing is widened beyond what the unit already asked for.
    """
    argv = entry.argv
    provider = _argument(argv, "--provider") or "dukascopy"
    price_basis = entry.price_basis or price_basis
    symbols = legacy_symbols(entry, maintenance_symbols=maintenance_symbols) or []
    recipe_ids, recipe_versions = _recipe_arguments(argv)
    recipes = sorted(recipe_ids | {f"{identifier}@{version}"
                                   for identifier in recipe_ids for version in recipe_versions})
    scope = provider_symbols(provider, symbols)
    symbols = list(scope.served)
    schedule = schedule_for(entry)
    if schedule is None or not symbols:
        # No guess: an entry whose trigger or scope cannot be established is
        # reported by the comparison instead of being imported with invented data.
        return []
    if not raw_only(entry) and not recipes:
        # A derived producer that names no recipes would import either every
        # derivable target (a widening) or no derived output at all (a silent
        # drop), so its output scope has to be supplied explicitly.
        return []
    targets = [] if raw_only(entry) else _derivable_targets(
        provider=provider, price_basis=price_basis, recipes=recipes)
    if not raw_only(entry) and not targets:
        # A derived entry whose recipes resolve to no output for this provider
        # would import a raw-only plan and silently drop what it produced.
        return []
    definitions = []
    for symbol in sorted(symbols):
        definitions.append({
            "provider": provider, "symbol": symbol,
            "raw_timeframe": DEFAULT_RAW_TIMEFRAME, "price_basis": price_basis,
            # Only targets whose chain actually resolves for this provider: an
            # imported plan must not promise a derivation the registry cannot do.
            "bar_timeframes": list(targets),
            "window_policy": {"mode": "continuous",
                              "history_start": _history_start(
                                  provider, DEFAULT_RAW_TIMEFRAME, now=now).isoformat()},
            "schedule": dict(schedule),
        })
    return definitions


def runner_module_of(entry: LegacyEntry) -> str | None:
    """The runner an entry reaches: its recorded field, else its own command.

    The field is what a host inventory reports; a unit file or a hand-written
    inventory names the module in the command instead, so both are read.
    """
    if entry.runner_module:
        return entry.runner_module
    return next((module for module in LEGACY_RUNNER_MODULES if module in entry.command), None)


def raw_only(entry: LegacyEntry) -> bool:
    """Whether an entry produced raw bars only, from the runner it reaches."""
    return runner_module_of(entry) != DERIVED_RUNNER_MODULE


def _history_start(provider: str, raw_timeframe: str, *, now: datetime | None = None) -> datetime:
    """The old runner's own tail window, expressed as the plan's history start.

    The legacy runner plans from its registered tail (two days for Dukascopy), so
    the imported plan starts where the old entry effectively started instead of
    asking for history the previous entry never fetched.  The clock is injectable
    so a dry run and the write it describes agree on the range (spec 5.1).
    """
    from .platform_registry import maintenance_policy_for

    policy = maintenance_policy_for(provider, raw_timeframe)
    moment = now or datetime.now(timezone.utc)
    return (moment - timedelta(days=max(1, policy.tail_days))).replace(microsecond=0)


def schedule_seconds(schedule: dict) -> float:
    """How often a schedule fires, for comparing two legacy cadences.

    A daily calendar is once a day; a plan with no automatic trigger (manual or
    one-shot) is least frequent.  Comparing the schedules directly used to raise
    ``KeyError`` as soon as a delay entry and a calendar entry covered the same
    instrument.
    """
    if "interval_seconds" in schedule:
        return float(schedule["interval_seconds"])
    if schedule.get("schedule") == "daily":
        return 86400.0
    return float("inf")


def schedule_shape(schedule: dict | None) -> tuple:
    """The part of a schedule an import has to preserve, as a comparable tuple.

    Comparing only the kind and the interval let a daily plan drift its wall-clock
    time or zone and still verify, which is exactly the drift the handover must
    not accept.  Volatile fields (a fixed-rate anchor) are deliberately excluded.
    """
    document = schedule or {}
    kind = document.get("schedule")
    if kind == "daily":
        return kind, document.get("timezone") or "UTC", document.get("local_time")
    if kind in {"fixed_rate", "fixed_delay"}:
        return kind, int(document.get("interval_seconds") or 0)
    return (kind,)


def merged_definitions(entries: list[LegacyEntry], *, price_basis: str = "bid",
                       maintenance_symbols: tuple[str, ...] = (),
                       now: datetime | None = None) -> list[dict]:
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
        for definition in plan_definitions(entry, price_basis=price_basis,
                                           maintenance_symbols=maintenance_symbols, now=now):
            key = (definition["provider"], definition["symbol"])
            current = grouped.get(key)
            if current is None:
                grouped[key] = {**definition, "sources": [entry.unit],
                                "cadences": {entry.unit: dict(definition["schedule"])}}
                continue
            current["bar_timeframes"] = sorted(set(current["bar_timeframes"])
                                               | set(definition["bar_timeframes"]))
            current["sources"].append(entry.unit)
            current["cadences"][entry.unit] = dict(definition["schedule"])
            # The more frequent cadence wins: a slower schedule would stop
            # producing what the faster entry used to produce.  Both are kept in
            # the comparison receipt so the operator can see the difference.
            if schedule_seconds(definition["schedule"]) < schedule_seconds(current["schedule"]):
                current["schedule"] = dict(definition["schedule"])
    return [grouped[key] for key in sorted(grouped)]


def import_entries(service: ProductionTasks, *, entries: list[LegacyEntry], actor: str,
                   apply: bool = False, price_basis: str = "bid",
                   maintenance_symbols: tuple[str, ...] = (),
                   now: datetime | None = None) -> dict:
    """Import legacy entries as paused plans; without ``apply`` nothing is written."""
    moment = now or datetime.now(timezone.utc)
    import_rows, created, skipped = [], [], []
    existing = {task["task_id"] for task in service.ledger.list_production_tasks(include_deleted=True)}
    for definition in merged_definitions(entries, price_basis=price_basis,
                                         maintenance_symbols=maintenance_symbols, now=moment):
        task_id = f"legacy-{definition['provider']}-{definition['symbol']}-{definition['raw_timeframe']}"
        row = {"task_id": task_id, "symbol": definition["symbol"],
               "sources": definition["sources"], "cadences": definition["cadences"],
               "bar_timeframes": definition["bar_timeframes"],
               "schedule": definition["schedule"]["schedule"],
               # A daily plan has no interval: the schedule object is the truth.
               "interval_seconds": definition["schedule"].get("interval_seconds")}
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
        except DefinitionError as exc:
            # One definition the registry refuses (an unapproved instrument, a
            # recipe that no longer exists) must not lose the other plans.
            skipped.append({**row, "reason": "invalid_definition", "errors": exc.errors})
            continue
        created.append({**row, "definition_version": plan["definition_version"]})
    return {"apply": apply, "created": created, "skipped": skipped,
            "rows": import_rows, "at": moment.isoformat()}


def verify_takeover(service: ProductionTasks, *, entries: list[LegacyEntry],
                    inventory: dict | None = None,
                    maintenance_symbols: tuple[str, ...] = (),
                    price_basis: str = "bid",
                    declared_unit_files: set[str] | None = None,
                    now: datetime | None = None) -> dict:
    """Post-conditions an operator must be able to show after a handover."""
    problems: list[str] = []
    rows = []
    for entry in entries:
        if legacy_symbols(entry, maintenance_symbols=maintenance_symbols) is None:
            problems.append(
                f"{entry.unit}: the instrument scope is unknown — pass --symbols or "
                f"DATACENTER_MAINTENANCE_SYMBOLS before trusting the comparison")
    for definition in merged_definitions(entries, price_basis=price_basis,
                                         maintenance_symbols=maintenance_symbols, now=now):
        task_id = f"legacy-{definition['provider']}-{definition['symbol']}-{definition['raw_timeframe']}"
        task = service.ledger.get_production_task(task_id)
        if task is None:
            problems.append(f"{task_id} was never imported")
            continue
        document = service.read(task_id)
        expected = definition["schedule"]
        actual = task["payload"].get("schedule") or {}
        if schedule_shape(actual) != schedule_shape(expected):
            problems.append(f"{task_id} schedule {actual} does not match the legacy {expected}")
        rows.append({"task_id": task_id, "desired_state": task["desired_state"],
                     "health": document["health"], "block_reason": document["block_reason"],
                     "schedule": actual})
    # Two writers for one output is the failure this whole handover exists to avoid.
    for task in service.ledger.list_production_tasks():
        if task["desired_state"] != "enabled":
            continue
        keys = [item["ownership_key"] for item in service.ledger.ownership_of(task["task_id"])]
        for holder in service.ledger.ownership_holders(keys):
            if holder.get("task_id") not in (None, task["task_id"]):
                problems.append(
                    f"{holder['ownership_key']} is held by {holder['task_id']} and {task['task_id']}")
    comparison = compare_entries(entries, inventory, maintenance_symbols=maintenance_symbols,
                                 price_basis=price_basis, declared_unit_files=declared_unit_files)
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
    unit_root = Path(args.unit_root) if args.unit_root else default_unit_root(
        manifest=settings.deployment_manifest)
    if unit_root is None:
        # Say so instead of reporting every host unit as host-only.
        print(json.dumps({"event": f"takeover_{args.command}",
                          "error": "declared_unit_root_not_found",
                          "hint": "pass --unit-root <release>/deploy/systemd"}, indent=2))
        return 2
    inventory = json.loads(Path(args.inventory).read_text()) if args.inventory else host_inventory()
    entries = planned_entries(unit_root, inventory)
    # The old runner's allowlist lived in the machine-level env file; the tool
    # reads the same setting so the imported scope is the scope that was real.
    maintenance_symbols = settings.maintenance_symbol_list()
    moment = datetime.now(timezone.utc)
    started = moment.isoformat()
    evidence_root = Path(args.evidence_root) if args.evidence_root else settings.evidence_root

    # The comparison needs to know which units the repository declares so a
    # host-only unit is visible as such (and not as a missing declaration).
    declared_units = {entry.unit for entry in declared_entries(unit_root)}
    declared_files = declared_unit_files(unit_root)
    if args.command == "plan":
        details = compare_entries(entries, inventory, declared_units=declared_units,
                                  declared_unit_files=declared_files,
                                  maintenance_symbols=maintenance_symbols,
                                  price_basis=args.price_basis)
        print(json.dumps({"event": "takeover_plan", **details}, indent=2, sort_keys=True))
        return 0

    from .runs.ledger import RunLedger

    service = ProductionTasks(RunLedger(settings.ledger_path), canonical_root=settings.canonical_root)
    if args.command == "import":
        result = import_entries(service, entries=entries, actor=args.actor, apply=args.apply,
                                price_basis=args.price_basis,
                                maintenance_symbols=maintenance_symbols, now=moment)
        result["comparison"] = compare_entries(entries, inventory,
                                               declared_units=declared_units,
                                               declared_unit_files=declared_files,
                                               maintenance_symbols=maintenance_symbols,
                                               price_basis=args.price_basis)
        path = _receipt(evidence_root, action="production_takeover_import",
                        result="pass", details=result, started_at=started)
        print(json.dumps({"event": "takeover_import", "applied": args.apply,
                          "created": len(result["created"]), "skipped": len(result["skipped"]),
                          "receipt": path}, indent=2, sort_keys=True))
        return 0

    report = verify_takeover(service, entries=entries, inventory=inventory,
                             maintenance_symbols=maintenance_symbols,
                             price_basis=args.price_basis,
                             declared_unit_files=declared_files, now=moment)
    path = _receipt(evidence_root, action="production_takeover_verify",
                    result="failed" if report["problems"] else "pass",
                    details=report, started_at=started)
    print(json.dumps({"event": "takeover_verify", "problems": report["problems"],
                      "receipt": path}, indent=2, sort_keys=True))
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
