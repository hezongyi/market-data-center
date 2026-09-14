"""Production task module: one interface for plan definitions, preview and change.

The WebUI HTTP adapter and the scheduler process both call into this module;
neither builds job payloads nor re-interprets policy on its own (spec 7.1).
Plan identity, ownership, schedule and version handling live here, while the
ledger stays responsible for durable, transactional state.
"""
from __future__ import annotations

import base64
import builtins
import hashlib
import hmac
import json
import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .catalog.manifest import PublicationError
from .catalog.snapshot import Catalog, snapshot_reference
from .control_plane import timeframe_delta
from .domain.models import DeriveJob, IngestJob
from .platform import coverage_from_catalog, ingest_window_payloads
from .platform_registry import REGISTRY, config_digest, maintenance_policy_for
from .runs.ledger import IdempotencyConflict, ProductionConflict, RunLedger
from .scheduler import MIN_INTERVAL_SECONDS, next_run_at, validate_schedule
from .transform import TIMEFRAMES, current_rows, resolve_session_profile
from .window_planner import exclude_planned_windows, missing_ranges, recent_gap_windows

RAW_DATASET = "provider_bars"
DERIVED_DATASET = "market_bars"
OBSERVATION_DATASET = "economic_observations"
DEFAULT_RAW_TIMEFRAME = "1m"
PREVIEW_RUNS = 5
DEFAULT_PAGE_SIZE = 50
DEFAULT_EXECUTION_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200
#: A coverage scan reaches slightly further back than the policy tail so a gap
#: that just left the tail is still decided by coverage, not by a stale cache.
COVERAGE_SCAN_MARGIN_DAYS = 1
#: Bounded gap debt: enough to keep a permanent provider omission visible
#: without letting a pathological history grow one plan's payload forever.
GAP_LIMIT = 50
#: One window expands into at most this many bucket runs; a window with more
#: holes than that defers the remainder instead of flooding the round.
DERIVE_RUNS_PER_WINDOW = 12

#: Commands accepted by :meth:`ProductionTasks.change` (spec 8).
CHANGE_COMMANDS = ("update", "pause", "resume", "run_now", "retry", "archive",
                   "copy", "delete", "acknowledge_drift")

#: Active plan states; archived and deleted plans have released their ownership.
ACTIVE_STATES = ("enabled", "paused")


class DefinitionError(ValueError):
    """Field-level definition errors, reported together so the wizard can point at fields."""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("; ".join(f"{item['field']}: {item['message']}" for item in errors))


def _as_utc(value, field: str) -> datetime:
    """Parse an ISO-8601 timestamp, rejecting a missing timezone (spec 5.1)."""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def ownership_key(*, dataset_id: str, provider: str | None = None, symbol: str | None = None,
                  timeframe: str | None = None, price_basis: str | None = None,
                  series_id: str | None = None) -> str:
    """Render the ownership key of one production output (spec 3.3)."""
    if dataset_id == OBSERVATION_DATASET:
        parts = [dataset_id, series_id]
    else:
        parts = [dataset_id, provider, symbol, timeframe, price_basis or "bid"]
    if any(not part for part in parts):
        raise ValueError(f"incomplete ownership identity for {dataset_id}")
    return ":".join(str(part) for part in parts)


def _recipe_document(recipe) -> dict:
    return {"recipe_id": recipe.recipe_id, "recipe_version": recipe.version,
            "input_dataset": recipe.input_dataset, "output_dataset": recipe.output_dataset,
            "source_timeframe": recipe.source_timeframe, "target_timeframe": recipe.target_timeframe,
            "session_profile": recipe.session_profile, "calendar_profile": recipe.calendar_profile,
            "aggregation": recipe.aggregation, "partial_bucket_policy": recipe.partial_bucket_policy,
            "missing_input_policy": recipe.missing_input_policy,
            "publication_policy": recipe.publication_policy,
            "input_recipe_id": recipe.input_recipe_id,
            "input_recipe_version": recipe.input_recipe_version}


def _output_recipe(target_timeframe: str, *, provider: str, price_basis: str):
    """Pick the registered recipe that materialises one output timeframe."""
    candidates = []
    for recipe in REGISTRY.recipes():
        if recipe.output_dataset != DERIVED_DATASET or recipe.target_timeframe != target_timeframe:
            continue
        if recipe.allowed_providers and provider not in recipe.allowed_providers:
            continue
        if recipe.allowed_price_bases and price_basis not in recipe.allowed_price_bases:
            continue
        candidates.append(recipe)
    return min(candidates, key=lambda item: (item.recipe_id, item.version)) if candidates else None


def _recipe_chain(target_timeframe: str, *, raw_timeframe: str, provider: str, price_basis: str,
                  seen: frozenset[str] = frozenset()) -> list[dict] | None:
    """Resolve the recipe chain from raw to one output, including intermediate hops.

    A weekly output depends on the daily recipe, so the chain is derived from
    the registry's declared ``input_recipe_id`` rather than from name ordering
    (spec 6.2).  A cycle is a registry defect and is reported as such.
    """
    if target_timeframe in seen:
        raise DefinitionError([{"field": "bar_timeframes",
                                "message": f"recipe dependency cycle at {target_timeframe}"}])
    recipe = _output_recipe(target_timeframe, provider=provider, price_basis=price_basis)
    if recipe is None:
        return None
    if recipe.input_dataset == RAW_DATASET:
        if recipe.source_timeframe != raw_timeframe:
            return None
        return [_recipe_document(recipe)]
    if recipe.input_recipe_id:
        parent = REGISTRY.recipe(recipe.input_recipe_id, recipe.input_recipe_version)
    else:
        parent = _output_recipe(recipe.source_timeframe, provider=provider, price_basis=price_basis)
    if parent is None:
        return None
    head = _recipe_chain(parent.target_timeframe, raw_timeframe=raw_timeframe, provider=provider,
                         price_basis=price_basis, seen=seen | {target_timeframe})
    return None if head is None else head + [_recipe_document(recipe)]


def _normalize_window_policy(policy, errors: list[dict], *, now: datetime) -> dict:
    policy = policy if isinstance(policy, dict) else {}
    mode = str(policy.get("mode") or "continuous")
    if mode not in {"continuous", "fixed"}:
        errors.append({"field": "window_policy.mode", "message": "mode must be continuous or fixed"})
        return {"mode": mode}
    if mode == "continuous":
        try:
            history_start = _as_utc(policy.get("history_start"), "window_policy.history_start")
        except ValueError as exc:
            errors.append({"field": "window_policy.history_start", "message": str(exc)})
            return {"mode": mode}
        if history_start > now:
            errors.append({"field": "window_policy.history_start",
                           "message": "history_start cannot be in the future"})
        tail_days = policy.get("tail_days")
        return {"mode": mode, "history_start": history_start.isoformat(),
                "tail_days": int(tail_days) if tail_days else None}
    normalized = {"mode": mode}
    bounds = {}
    for field in ("start", "end"):
        try:
            bounds[field] = _as_utc(policy.get(field), f"window_policy.{field}")
        except ValueError as exc:
            errors.append({"field": f"window_policy.{field}", "message": str(exc)})
    if len(bounds) == 2 and bounds["start"] >= bounds["end"]:
        errors.append({"field": "window_policy.end", "message": "end must be after start"})
    normalized.update({field: value.isoformat() for field, value in bounds.items()})
    return normalized


def normalize_definition(definition: dict, *, now: datetime) -> dict:
    """Validate a plan definition and return its canonical stored form.

    All field errors are collected so the wizard can highlight every offending
    field at once; nothing is written by this function.
    """
    now = _as_utc(now, "now")
    definition = definition if isinstance(definition, dict) else {}
    errors: list[dict] = []
    provider = str(definition.get("provider") or "").strip()
    capability = None
    try:
        capability = REGISTRY.capability(provider)
    except ValueError as exc:
        errors.append({"field": "provider", "message": str(exc)})
    symbol = str(definition.get("symbol") or "").strip()
    instrument = None
    if capability is not None:
        try:
            instrument = REGISTRY.instrument(provider, symbol)
            if not instrument.approved:
                errors.append({"field": "symbol",
                               "message": f"instrument {provider}/{symbol} is not approved"})
        except ValueError as exc:
            errors.append({"field": "symbol", "message": str(exc)})

    raw_timeframe = str(definition.get("raw_timeframe") or DEFAULT_RAW_TIMEFRAME)
    if capability is not None:
        supported = tuple(capability.maintenance_timeframes or capability.timeframes)
        if raw_timeframe not in supported:
            errors.append({"field": "raw_timeframe",
                           "message": f"{provider} cannot maintain {raw_timeframe}"})
    price_basis = str(definition.get("price_basis") or
                      (capability.price_bases[0] if capability and capability.price_bases else "bid"))
    if capability is not None and price_basis not in capability.price_bases:
        errors.append({"field": "price_basis",
                       "message": f"{provider} does not publish {price_basis}"})

    requested = definition.get("bar_timeframes") or []
    if not isinstance(requested, (list, tuple)) or any(not isinstance(item, str) for item in requested):
        errors.append({"field": "bar_timeframes", "message": "bar_timeframes must be a list of timeframes"})
        requested = []
    bar_timeframes: list[str] = []
    chains: dict[str, list[dict]] = {}
    # Recipe resolution depends on the provider capability, not on the symbol,
    # so an unusable output is reported even when the symbol is also wrong.
    if capability is not None:
        for timeframe in requested:
            chain = _recipe_chain(timeframe, raw_timeframe=raw_timeframe, provider=provider,
                                  price_basis=price_basis)
            if chain is None:
                errors.append({"field": "bar_timeframes",
                               "message": f"no registered recipe produces {timeframe} from "
                                          f"{raw_timeframe} for {provider}"})
                continue
            if timeframe not in bar_timeframes:
                bar_timeframes.append(timeframe)
                chains[timeframe] = chain

    try:
        schedule = validate_schedule(definition.get("schedule") or {"schedule": "manual"}, now=now)
    except ValueError as exc:
        errors.append({"field": "schedule", "message": str(exc)})
        schedule = definition.get("schedule") if isinstance(definition.get("schedule"), dict) else {}

    window_policy = _normalize_window_policy(definition.get("window_policy"), errors, now=now)
    concurrency = definition.get("concurrency", 1)
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
        errors.append({"field": "concurrency", "message": "concurrency must be a positive integer"})
        concurrency = 1
    if errors:
        raise DefinitionError(errors)
    return {
        "provider": provider,
        "symbol": symbol,
        "asset_class": instrument.asset_class,
        "raw_timeframe": raw_timeframe,
        "price_basis": price_basis,
        "bar_timeframes": bar_timeframes,
        "window_policy": window_policy,
        "schedule": schedule,
        "concurrency": concurrency,
        "recipe_chains": chains,
    }


def ownership_keys(definition: dict) -> list[str]:
    """Every output ownership key a plan holds (spec 3.3)."""
    keys = [ownership_key(dataset_id=RAW_DATASET, provider=definition["provider"],
                          symbol=definition["symbol"], timeframe=definition["raw_timeframe"],
                          price_basis=definition["price_basis"])]
    for timeframe in definition.get("bar_timeframes", ()):
        keys.append(ownership_key(dataset_id=DERIVED_DATASET, provider=definition["provider"],
                                  symbol=definition["symbol"], timeframe=timeframe,
                                  price_basis=definition["price_basis"]))
    return keys


def dependencies(definition: dict) -> list[dict]:
    """The ordered, de-duplicated recipe chain every requested output needs."""
    ordered: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for timeframe in definition.get("bar_timeframes", ()):
        for step in definition.get("recipe_chains", {}).get(timeframe, ()):
            identity = (step["recipe_id"], step["recipe_version"])
            if identity not in seen:
                seen.add(identity)
                ordered.append(step)
    return ordered


def config_facts(definition: dict) -> dict:
    """Registry-derived configuration a plan version was resolved against.

    Recomputed on every reconciliation and compared with the persisted digest;
    a difference means the registry changed underneath a plan and the plan must
    stop dispatching until the drift is acknowledged (spec 5.3, AC21).
    """
    capability = REGISTRY.capability(definition["provider"])
    instrument = REGISTRY.instrument(definition["provider"], definition["symbol"])
    policy = maintenance_policy_for(definition["provider"], definition["raw_timeframe"])
    return {
        "dataset": RAW_DATASET,
        "provider": capability.provider,
        "capability": {
            "timeframes": list(capability.timeframes),
            "maintenance_timeframes": list(capability.maintenance_timeframes or capability.timeframes),
            "price_bases": list(capability.price_bases),
            "session_profile": capability.session_profile,
            "max_window_days": capability.max_window_days,
        },
        "instrument": {
            "provider": instrument.provider, "symbol": instrument.symbol,
            "asset_class": instrument.asset_class, "session_profile": instrument.session_profile,
            "calendar_profile": instrument.calendar_profile, "approved": instrument.approved,
        },
        "recipes": [{"recipe_id": item["recipe_id"], "recipe_version": item["recipe_version"],
                     "target_timeframe": item["target_timeframe"],
                     "session_profile": item["session_profile"],
                     "calendar_profile": item["calendar_profile"],
                     "policy": item["publication_policy"]} for item in dependencies(definition)],
        "maintenance_policy": {
            "policy_id": policy.policy_id, "max_window_days": policy.max_window_days,
            "tail_days": policy.tail_days, "shard_days": policy.shard_days,
            "shard_minutes": policy.shard_minutes,
            "closed_bar_lag_minutes": policy.closed_bar_lag_minutes,
            "gap_retry_cooldown_minutes": policy.gap_retry_cooldown_minutes,
        },
    }


def schedule_cursor(definition: dict) -> dict:
    """The scheduler-facing schedule parameters of a stored definition."""
    return dict(definition.get("schedule") or {})


def next_runs(definition: dict, *, now: datetime, count: int = PREVIEW_RUNS) -> list[str]:
    """The next ``count`` concrete UTC slots, or fewer when the plan has no fixed time.

    A ``fixed_delay`` plan has no absolute next time until a run completes, so
    it returns an empty list rather than a fabricated date (spec 5.1).
    """
    schedule = schedule_cursor(definition)
    kind = schedule.get("schedule")
    runs: list[datetime] = []
    cursor = _as_utc(now, "now")
    for _ in range(max(1, count)):
        value = next_run_at(
            schedule=kind, now=cursor,
            anchor=_optional_utc(schedule.get("anchor")),
            interval_seconds=schedule.get("interval_seconds"),
            run_at=_optional_utc(schedule.get("run_at")),
            completed_at=_optional_utc(schedule.get("completed_at")),
            timezone_name=schedule.get("timezone"), local_time=schedule.get("local_time"),
            # The first occurrence may be due right now; every following one
            # must be strictly later than the previous.
            strict=bool(runs))
        if value is None or (runs and value <= runs[-1]):
            break
        runs.append(value)
        if kind == "once":
            break
        cursor = value
    return [item.isoformat() for item in runs]


def _optional_utc(value) -> datetime | None:
    return None if value is None else _as_utc(value, "schedule value")


class ProductionTasks:
    """Plan registry service: preview, create, read, list and change."""

    def __init__(self, ledger: RunLedger, *, cursor_secret: str | None = None,
                 cursor_ttl_seconds: float = 3600.0, canonical_root=None):
        self.ledger = ledger
        # Needed to resolve upstream snapshots for derived steps; a service
        # without one can still validate and manage plans.
        self.canonical_root = None if canonical_root is None else Path(canonical_root)
        secret = cursor_secret or f"market-data-center-production:{ledger.path}"
        self._cursor_secret = hashlib.sha256(secret.encode()).digest()
        self.cursor_ttl_seconds = cursor_ttl_seconds

    # -- cursors ---------------------------------------------------------
    def _encode_cursor(self, payload: dict) -> str:
        raw = json.dumps({**payload, "expires_at": time.time() + self.cursor_ttl_seconds},
                         sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(self._cursor_secret, raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw + signature).decode().rstrip("=")

    def _decode_cursor(self, cursor: str) -> dict:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
                raise ValueError("cursor is invalid")
            message, signature = raw[:-32], raw[-32:]
            expected = hmac.new(self._cursor_secret, message, hashlib.sha256).digest()
            if len(signature) != 32 or not hmac.compare_digest(signature, expected):
                raise ValueError("cursor is invalid")
            payload = json.loads(message)
        except Exception as exc:
            raise ProductionConflict("cursor_error", "cursor is invalid or has been tampered with") from exc
        if payload.get("expires_at", 0) < time.time():
            raise ProductionConflict("cursor_error", "cursor has expired")
        return payload

    # -- read paths ------------------------------------------------------
    def preview(self, definition: dict, *, now: datetime | None = None) -> dict:
        """Side-effect-free projection of what saving this plan would mean (spec 8)."""
        now = now or datetime.now(timezone.utc)
        try:
            normalized = normalize_definition(definition, now=now)
        except DefinitionError as exc:
            return {"definition": definition, "validation": {"errors": exc.errors},
                    "submittable": False, "ownership_keys": [], "dependencies": [],
                    "schedule": {"kind": (definition or {}).get("schedule", {}).get("schedule", "manual"),
                                 "next_runs": [], "rule": None},
                    "policy": None, "conflicts": [], "dispatch_enabled": False}
        keys = ownership_keys(normalized)
        held = {item["ownership_key"]: item["task_id"] for item in self.ledger.ownership_holders(keys)}
        conflicts = [{"ownership_key": key, "task_id": held[key]} for key in keys if key in held]
        preview_definition = {key: value for key, value in normalized.items() if key != "recipe_chains"}
        policy = maintenance_policy_for(normalized["provider"], normalized["raw_timeframe"])
        return {
            "definition": preview_definition,
            "validation": {"errors": [], "warnings": self._window_warnings(normalized, policy)},
            "submittable": not conflicts,
            "ownership_keys": keys,
            "dependencies": dependencies(normalized),
            "schedule": {
                "kind": normalized["schedule"].get("schedule"),
                "interval_seconds": normalized["schedule"].get("interval_seconds"),
                "next_runs": next_runs(normalized, now=now),
                "rule": (f"完成后 {normalized['schedule']['interval_seconds'] // 60} 分钟"
                         if normalized["schedule"].get("schedule") == "fixed_delay" else None),
            },
            "policy": {
                "policy_id": policy.policy_id, "max_window_days": policy.max_window_days,
                "tail_days": policy.tail_days, "shard_minutes": policy.shard_minutes,
                "closed_bar_lag_minutes": policy.closed_bar_lag_minutes,
                "gap_retry_cooldown_minutes": policy.gap_retry_cooldown_minutes,
            },
            "minimum_interval_seconds": MIN_INTERVAL_SECONDS,
            "config_digest": config_digest(config_facts(normalized)),
            "conflicts": conflicts,
            "dispatch_enabled": False,
        }

    @staticmethod
    def _window_warnings(definition: dict, policy) -> list[str]:
        warnings = []
        window = definition.get("window_policy") or {}
        if window.get("mode") == "continuous" and window.get("history_start"):
            span = (datetime.now(timezone.utc) - _as_utc(window["history_start"], "history_start")).days
            if span > policy.max_window_days:
                warnings.append(
                    f"initial backfill spans {span} days, beyond the unattended limit of "
                    f"{policy.max_window_days} days; it will be planned in bounded batches")
        return warnings

    def read(self, task_id: str) -> dict | None:
        task = self.ledger.get_production_task(task_id)
        if task is None:
            return None
        executions = self.ledger.list_production_executions(task["task_id"], limit=5)
        active = next((item for item in executions
                       if item["state"] in RunLedger.ACTIVE_EXECUTION_STATES), None)
        definition = task.get("payload") or {}
        progress = self.ledger.production_progress(task["task_id"])
        return {
            **task,
            "ownership": self.ledger.ownership_of(task["task_id"]),
            "executions": executions,
            "current_execution": active,
            "health": self._health(task, active),
            "tombstone": bool(task["deleted_at"]),
            "schedule": {"kind": (definition.get("schedule") or {}).get("schedule"),
                         "next_run_at": task.get("next_run_at")},
            # The recorded progress, not a guess: how far raw has been planned
            # and derived, what is still owed, and how the last round ended.
            "progress": self._progress_view(progress),
        }

    @staticmethod
    def _progress_view(progress: dict | None) -> dict:
        progress = progress or {}
        return {
            "raw_frontier": progress.get("frontier"),
            "provider_bounded_end": progress.get("effective_end"),
            "backlog": bool(progress.get("backlog")),
            "derived_cursor": progress.get("derived_cursor"),
            "last_outcome": progress.get("last_outcome"),
            "last_finished_at": progress.get("last_finished_at"),
            "last_execution_id": progress.get("last_execution_id"),
            "recompute_pending": len(progress.get("recompute") or []),
            # What the provider has actually shown, next to what the plan has
            # planned: the two answer different questions and the console must
            # not confuse a planned boundary with live freshness.
            "observed_boundary": progress.get("observed_boundary"),
            "complete_boundary": progress.get("complete_boundary"),
            "gaps": [
                {"window_start": item.get("window_start"), "window_end": item.get("window_end"),
                 "state": item.get("state"), "attempts": int(item.get("attempts") or 0),
                 "reason": item.get("reason")}
                for item in (progress.get("gaps") or [])
            ],
            "deferred_derived": [item.get("step") for item in (progress.get("deferred_derived") or [])],
            "recorded": bool(progress),
            "note": "Recorded planning boundaries, not live provider freshness.",
        }

    def _health(self, task: dict, active_execution: dict | None) -> str:
        if task["deleted_at"]:
            return "deleted"
        if task.get("health") == "config_drift":
            return "config_drift"
        if task["desired_state"] == "paused":
            return "paused"
        if task["desired_state"] == "archived":
            return "archived"
        if active_execution is not None and active_execution["state"] in {"pausing", "paused"}:
            return "attention"
        return "healthy"

    def executions(self, task_id: str, *, page_size: int | None = None,
                   cursor: str | None = None) -> dict:
        """A plan's round history, cursored and bound to that plan (spec 8)."""
        effective = DEFAULT_EXECUTION_PAGE_SIZE if page_size is None else page_size
        if effective < 1 or effective > MAX_PAGE_SIZE:
            raise ProductionConflict("page_size_error", f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        filters = {"task_id": task_id}
        before = None
        if cursor:
            payload = self._decode_cursor(cursor)
            if payload.get("filters") != filters:
                raise ProductionConflict("cursor_error", "cursor does not match the requested plan")
            before = (payload["created_at"], payload["execution_id"])
        page = self.ledger.list_production_executions_page(task_id, page_size=effective, before=before)
        next_cursor = None
        if page["has_more"] and page["items"]:
            last = page["items"][-1]
            next_cursor = self._encode_cursor({"created_at": last["created_at"],
                                               "execution_id": last["execution_id"], "filters": filters})
        return {"executions": page["items"],
                "page": {"count": len(page["items"]), "page_size": effective,
                         "next_cursor": next_cursor, "filters": filters}}

    def list(self, *, provider: str | None = None, symbol: str | None = None,
             desired_state: str | None = None, include_deleted: bool = False,
             page_size: int | None = None, cursor: str | None = None) -> dict:
        """Cursor-paginated plan list; the cursor is bound to the filter set (spec 8)."""
        effective = DEFAULT_PAGE_SIZE if page_size is None else page_size
        if effective < 1 or effective > MAX_PAGE_SIZE:
            raise ProductionConflict("page_size_error", f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        filters = {"provider": provider, "symbol": symbol, "desired_state": desired_state,
                   "include_deleted": bool(include_deleted)}
        before = None
        if cursor:
            payload = self._decode_cursor(cursor)
            if payload.get("filters") != filters:
                raise ProductionConflict("cursor_error", "cursor does not match the requested filters")
            before = (payload["updated_at"], payload["task_id"])
        page = self.ledger.list_production_tasks_page(
            provider=provider, symbol=symbol, desired_state=desired_state,
            include_deleted=include_deleted, page_size=effective, before=before)
        items = []
        for task in page["items"]:
            items.append(self.read(task["task_id"]))
        next_cursor = None
        if page["has_more"] and page["items"]:
            last = page["items"][-1]
            next_cursor = self._encode_cursor({"updated_at": last["updated_at"],
                                               "task_id": last["task_id"], "filters": filters})
        return {"tasks": items, "page": {"count": len(items), "page_size": effective,
                                         "next_cursor": next_cursor, "filters": filters}}

    def _resolve(self, task_id: str) -> dict:
        task = self.ledger.get_production_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _apply(self, task_id: str, command: str, action, *, request: dict | None,
               idempotency_key: str | None, actor: str | None, audit_action: str,
               request_id: str | None = None):
        """Run one idempotent action and audit a refusal the same way the API would.

        A refused action leaves no trace in the ledger otherwise, and "who tried
        to delete what and was told no" is exactly the audit question that gets
        asked later (spec 5.4, 16).
        """
        try:
            return self.ledger.production_idempotent(
                idempotency_key, task_id=task_id, command=command, request=request, actor=actor,
                audit_action=audit_action, action=action)
        except (KeyError, ProductionConflict, IdempotencyConflict) as exc:
            self.ledger.record_write_audit({
                "action": audit_action, "actor": actor, "request_id": request_id, "task_id": task_id,
                "outcome": "rejected", "code": getattr(exc, "code", "not_found"),
                "message": str(exc) or command})
            raise

    # -- write paths -----------------------------------------------------
    def create(self, *, definition: dict, name: str, task_id: str | None = None,
               alias: str | None = None, desired_state: str = "paused",
               actor: str | None = None, request_id: str | None = None,
               idempotency_key: str | None = None, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        normalized = normalize_definition(definition, now=now)
        name = str(name or "").strip()
        if not name:
            raise DefinitionError([{"field": "name", "message": "name is required"}])
        identity = task_id or str(uuid4())
        scheduled = next_runs(normalized, now=now, count=1)
        # The idempotency fingerprint covers the content of the request only:
        # a per-request id would make an honest replay look like a new request.
        record = {
            "name": name, "alias": alias, "desired_state": desired_state, "definition": normalized,
            "ownership_keys": ownership_keys(normalized),
        }

        audit = {"action": "production.task.create", "actor": actor, "request_id": request_id}

        def action(conn):
            stored = {key: value for key, value in normalized.items() if key != "recipe_chains"}
            return self.ledger.create_production_task(
                task_id=identity, name=name, payload=stored,
                ownership_keys=record["ownership_keys"], desired_state=desired_state, alias=alias,
                config_digest=config_digest(config_facts(normalized)),
                provider=normalized["provider"], symbol=normalized["symbol"],
                next_run_at=scheduled[0] if scheduled else None, conn=conn, audit=audit)

        return self._apply(identity, "create", action, request=record,
                           idempotency_key=idempotency_key, actor=actor,
                           audit_action="production.task.create", request_id=request_id)

    def change(self, task_id: str, command: str, *, definition: dict | None = None,
               expected_version: int | None = None, actor: str | None = None,
               request_id: str | None = None, idempotency_key: str | None = None,
               name: str | None = None, alias: str | None = None,
               now: datetime | None = None) -> dict:
        """Apply one management command, with idempotency and optimistic versioning."""
        if command not in CHANGE_COMMANDS:
            raise ProductionConflict("unsupported_command", f"unsupported production task command: {command}")
        now = now or datetime.now(timezone.utc)
        request = {"task_id": task_id, "command": command, "definition": definition,
                   "expected_version": expected_version, "name": name, "alias": alias}
        audit_action = f"production.task.{command}"
        audit = {"action": audit_action, "actor": actor, "request_id": request_id}

        if command == "pause" or command == "resume" or command == "archive":
            target = {"pause": "paused", "resume": "enabled", "archive": "archived"}[command]
            return self._apply(task_id, command,
                               lambda conn: self.ledger.set_production_task_state(
                                   task_id, target, expected_version=expected_version, conn=conn,
                                   audit=audit),
                               request=request, idempotency_key=idempotency_key, actor=actor,
                               audit_action=audit_action, request_id=request_id)

        if command == "delete":
            return self._apply(task_id, command,
                               lambda conn: self.ledger.delete_production_task(
                                   task_id, conn=conn, audit=audit),
                               request=request, idempotency_key=idempotency_key, actor=actor,
                               audit_action=audit_action)

        audit = {"action": audit_action, "actor": actor, "request_id": request_id}
        if command == "update":
            if expected_version is None:
                raise ProductionConflict("expected_version_required",
                                         "editing a definition requires expected_version")
            if definition is None:
                raise DefinitionError([{"field": "definition", "message": "definition is required"}])
            current = self._resolve(task_id)
            merged = {**(current.get("payload") or {}), **definition}
            normalized = normalize_definition(merged, now=now)
            stored = {key: value for key, value in normalized.items() if key != "recipe_chains"}
            keys = ownership_keys(normalized)
            scheduled = next_runs(normalized, now=now, count=1)

            def action(conn):
                # Changing the outputs changes which ownership keys the plan
                # holds; the swap has to stay unique at every instant.
                self.ledger.replace_task_ownership(conn, task_id, keys,
                                                  state=current["desired_state"])
                result = self.ledger.update_production_task(
                    task_id, stored, expected_version=expected_version, conn=conn,
                    config_digest=config_digest(config_facts(normalized)),
                    name=name, alias=alias, audit=audit)
                self.ledger.set_task_next_run_at(
                    task_id=task_id, next_run_at=scheduled[0] if scheduled else None, conn=conn)
                return result

            return self._apply(task_id, command, action, request=request,
                               idempotency_key=idempotency_key, actor=actor,
                               audit_action=audit_action, request_id=request_id)

        if command == "copy":
            source = self._resolve(task_id)
            merged = {**(source.get("payload") or {}), **(definition or {})}
            copied = self.create(definition=merged, name=name or f"{source['name']} (copy)",
                                 alias=alias, desired_state="paused", actor=actor,
                                 request_id=request_id, idempotency_key=idempotency_key, now=now)
            return {"copied_from": source["task_id"], **copied}

        if command == "run_now":
            return self._apply(task_id, command, lambda conn: self._run_now(conn, task_id, now=now),
                               request=request, idempotency_key=idempotency_key, actor=actor,
                               audit_action=audit_action, request_id=request_id)

        if command == "acknowledge_drift":
            current = self._resolve(task_id)
            digest = config_digest(config_facts(normalize_definition(current.get("payload") or {},
                                                                    now=now)))
            return self._apply(task_id, command,
                               lambda conn: self.ledger.acknowledge_config_drift(
                                   conn, task_id, expected_version=expected_version, digest=digest),
                               request=request, idempotency_key=idempotency_key, actor=actor,
                               audit_action=audit_action, request_id=request_id)

        raise ProductionConflict("unsupported_command", f"{command} is not available in this phase")

    RECOMPUTE_LIMIT = 50

    def record_recompute(self, task_id: str, *, window_start: str, window_end: str,
                         reason: str) -> dict:
        """Add a derived range to the persisted recompute set (AC12).

        The set is data, not a process-local event: a repaired window stays owed
        until a later reconciliation actually re-derives it, and the size is
        bounded so a pathological repair loop cannot grow it without limit.
        """
        progress = self.ledger.production_progress(task_id) or {}
        pending = [item for item in progress.get("recompute") or []
                   if not (item["window_start"] == window_start and item["window_end"] == window_end)]
        pending.append({"window_start": window_start, "window_end": window_end, "reason": reason,
                        "added_at": datetime.now(timezone.utc).isoformat(), "attempts": 0})
        # Newest repairs win when the set is at its bound.
        return self.ledger.record_progress(task_id, {"recompute": pending[-self.RECOMPUTE_LIMIT:]})

    def _recompute_ranges(self, task_id: str) -> builtins.list[dict]:
        return list((self.ledger.production_progress(task_id) or {}).get("recompute") or [])

    RAW_PARTS_MEMORY = 200
    EXTERNAL_PARTS_PER_TICK = 5

    def _record_external_publications(self, *, task: dict, definition: dict) -> builtins.list[dict]:
        """Notice raw this plan did not publish, and owe the derivation it invalidates.

        The plan remembers the raw parts it has already seen.  A part it has not
        seen was published by another governed entry point (a manual repair, or
        another task); if a completed derived step covered that window before the
        part's run finished, the derived output is stale and belongs in the
        recompute set (spec 6.2, 6.3).
        """
        if self.canonical_root is None:
            return []
        try:
            snapshot = Catalog(self.canonical_root).resolve(
                RAW_DATASET, {"provider": definition["provider"], "symbol": definition["symbol"],
                              "timeframe": definition["raw_timeframe"]})
        except (PublicationError, ValueError):
            return []
        progress = self.ledger.production_progress(task["task_id"]) or {}
        seen = progress.get("raw_parts")
        current = [f"{part.run_id}:{part.path.name}" for part in snapshot.parts]
        if seen is None:
            # First observation is a baseline, never a repair: a plan adopted
            # over an existing raw layer must not recompute all of history.
            self.ledger.record_progress(task["task_id"],
                                        {"raw_parts": current[-self.RAW_PARTS_MEMORY:]})
            return []
        fresh = [entry for entry in current if entry not in set(seen)]
        recorded = []
        for entry in fresh[:self.EXTERNAL_PARTS_PER_TICK]:
            run_id = entry.split(":", 1)[0]
            try:
                run = self.ledger.get(run_id)
            except KeyError:
                continue
            start, end, finished_at = run.get("start"), run.get("end"), run.get("finished_at")
            if not start or not end:
                continue
            if not self.ledger.stale_derived_steps(
                    task["task_id"], window_start=str(start), window_end=str(end),
                    after_created_at=str(finished_at or run.get("created_at") or "")):
                continue
            self.record_recompute(task["task_id"], window_start=str(start), window_end=str(end),
                                  reason="raw_published_outside_the_plan")
            recorded.append({"run_id": run_id, "start": str(start), "end": str(end)})
        if fresh:
            # Remember everything observed, even what was not examined this tick,
            # so the backlog cannot be re-detected forever as "new".
            self.ledger.record_progress(task["task_id"],
                                        {"raw_parts": current[-self.RAW_PARTS_MEMORY:]})
        return recorded

    def _consume_recompute(self, *, task: dict, definition: dict, ranges: builtins.list[dict],
                           step_budget: int) -> tuple[builtins.list[dict], builtins.list[str]]:
        """Plan the repaired ranges; return what stays owed and what was planned."""
        remaining: builtins.list[dict] = []
        planned: builtins.list[str] = []
        for item in ranges:
            if len(planned) >= max(1, step_budget):
                remaining.append(item)
                continue
            # Attach to the round in flight when there is one: a plan may only
            # ever have a single non-terminal execution.
            active = self.ledger.active_execution_for_task(task["task_id"])
            execution_id = active["execution_id"] if active else str(uuid4())
            steps, deferred, _not_ready = self._plan_derived_windows(
                task=task, definition=definition, execution_id=execution_id,
                windows=[(item["window_start"], item["window_end"])], step_budget=1)
            if not steps and not deferred:
                # The window is already planned for this round (the dependency
                # closure reached it first), so nothing is owed any more.
                continue
            if not steps:
                # Still owed: an input that is genuinely unavailable must stay
                # visible with its attempt count instead of disappearing.
                remaining.append({**item, "attempts": int(item.get("attempts") or 0) + 1,
                                  "deferred": deferred})
                continue
            try:
                self.ledger.accept_execution_plan(
                    execution_id=execution_id, steps=steps, task_id=task["task_id"],
                    definition_version=task["definition_version"], trigger_source="recompute",
                    audit={"action": "production.execution.recompute", "actor": "system:scheduler",
                           "request_id": None})
            except ProductionConflict:
                remaining.append({**item, "attempts": int(item.get("attempts") or 0) + 1})
                continue
            planned.extend(step["dedupe_key"] for step in steps)
        return remaining, planned

    def reconcile_publications(self, *, limit: int = 20, step_budget: int = 8) -> dict:
        """Derive what the published raw layer has made possible, from a cursor.

        A round that crashes after its raw windows publish, or raw published by
        another governed entry point, still has to produce its derived outputs:
        the cursor records how far derivation has been reconciled, the boundary
        records how far raw has actually been published, and the gap between
        them is planned here on the next tick (spec 6.2, AC10).
        """
        planned, deferred = [], []
        if self.canonical_root is None:
            return {"planned": planned, "deferred": deferred}
        checked = 0
        for task in self.ledger.list_production_tasks():
            if checked >= max(1, limit):
                break
            definition = task.get("payload") or {}
            if task["desired_state"] != "enabled" or task["deleted_at"]:
                continue
            if not definition.get("bar_timeframes"):
                continue
            checked += 1
            # The boundary is read from step rows, so they must reflect the run
            # outcomes first.
            self.ledger.refresh_task_steps(task["task_id"])
            # Raw published by another governed entry point is detected before the
            # debt is consumed, so a repair found now is planned in this same tick.
            self._record_external_publications(task=task, definition=definition)
            progress = self.ledger.production_progress(task["task_id"]) or {}
            # Repaired windows are owed whatever the publication boundary says:
            # they sit behind the cursor, so nothing else would plan them again.
            repaired = self._recompute_ranges(task["task_id"])
            if repaired:
                remaining, repaired_planned = self._consume_recompute(
                    task=task, definition=definition, ranges=repaired, step_budget=step_budget)
                self.ledger.record_progress(task["task_id"], {"recompute": remaining})
                planned.extend(repaired_planned)
            boundary = self.ledger.completed_raw_boundary(task["task_id"])
            if boundary is None:
                continue
            cursor = progress.get("derived_cursor") or _default_cursor(definition)
            if cursor is None:
                continue
            window_start = _as_utc(cursor, "derived_cursor")
            window_end = _as_utc(boundary, "publication_boundary")
            if window_end <= window_start:
                continue
            # Attach to the round already in flight when there is one: a plan
            # may only ever have a single non-terminal execution.
            active = self.ledger.active_execution_for_task(task["task_id"])
            execution_id = active["execution_id"] if active else str(uuid4())
            steps, deferred_here, _not_ready = self._plan_derived_windows(
                task=task, definition=definition, execution_id=execution_id,
                windows=[(window_start.isoformat(), window_end.isoformat())],
                step_budget=step_budget, skip_completed=True)
            deferred.extend(deferred_here)
            if steps:
                try:
                    self.ledger.accept_execution_plan(
                        execution_id=execution_id, steps=steps, task_id=task["task_id"],
                        definition_version=task["definition_version"], trigger_source="reconcile",
                        audit={"action": "production.execution.reconcile", "actor": "system:scheduler",
                               "request_id": None})
                except ProductionConflict:
                    # The round closed or paused between planning and acceptance:
                    # the work stays owed and is planned again on the next tick.
                    deferred.extend(step["dedupe_key"] for step in steps)
                    continue
                planned.extend(step["dedupe_key"] for step in steps)
            # The cursor advances over windows that are covered by a persisted
            # step, whether this pass planned it or an earlier one did; a
            # deferred window is never skipped.
            covered = sorted(((_as_utc(step["window_start"], "window_start"),
                               _as_utc(step["window_end"], "window_end"))
                              for step in self.ledger.refresh_execution_steps(execution_id)
                              if step["stage"].startswith("derive:") and step["window_start"]
                              and step["window_end"]
                              and _as_utc(step["window_end"], "window_end") <= window_end),
                             key=lambda item: item[0])
            # Advance only over contiguous coverage: a bucket whose input was
            # incomplete is not planned, so the cursor has to stay behind it or
            # the missing derived output would never be planned again.
            reached = window_start
            for start_at, end_at in covered:
                if start_at > reached:
                    break
                reached = max(reached, end_at)
            if reached > window_start:
                self.ledger.record_progress(task["task_id"], {
                    "derived_cursor": reached.isoformat(),
                    "derived_reconciled_at": datetime.now(timezone.utc).isoformat(),
                })
        return {"planned": planned, "deferred": deferred}

    def _plan_derived_windows(self, *, task: dict, definition: dict, execution_id: str,
                              windows: list[tuple[str, str]], step_budget: int,
                              skip_completed: bool = False) -> tuple[list[dict], list[str], list[str]]:
        """Plan derive steps for explicit windows, in recipe dependency order.

        Each window is split into contiguous runs of whole target buckets whose
        input is complete in the accepted snapshot.  A bucket with incomplete
        input is not planned at all and is reported as deferred with its reason:
        it therefore cannot hold back the buckets that are complete, and it is
        planned again as soon as the missing input is repaired (spec 5.5, AC12).
        """
        targets = definition.get("bar_timeframes") or []
        budget = max(1, step_budget)
        planned: list[dict] = []
        deferred: list[str] = []
        not_ready: list[str] = []
        existing = {item.get("dedupe_key") for item in self.ledger.list_production_steps(execution_id)}
        # Forward planning is idempotent across rounds; the recompute path is the
        # one that deliberately derives a window again.
        published = (self.ledger.completed_derived_windows(task["task_id"]) if skip_completed
                     else set())
        for window_start, window_end in windows:
            chain_documents = []
            for target in targets:
                try:
                    chain = _recipe_chain(target, raw_timeframe=definition.get("raw_timeframe")
                                          or DEFAULT_RAW_TIMEFRAME,
                                          provider=definition["provider"],
                                          price_basis=definition.get("price_basis") or "bid")
                except DefinitionError:
                    chain = None
                for item in chain or []:
                    if item not in chain_documents:
                        chain_documents.append(item)
            for recipe_document in chain_documents:
                window_identity = (f"derive:{recipe_document['recipe_id']}:"
                                   f"{_as_utc(window_start, 'window_start').isoformat()}:"
                                   f"{_as_utc(window_end, 'window_end').isoformat()}")
                prepared = self._derive_runs(definition=definition, recipe_document=recipe_document,
                                             window_start=window_start, window_end=window_end)
                if prepared is None:
                    deferred.append(window_identity)
                    continue
                runs, blocked = prepared
                for run_start, run_end in blocked:
                    identity = f"derive:{recipe_document['recipe_id']}:{run_start}:{run_end}"
                    deferred.append(identity)
                    not_ready.append(identity)
                for run_start, run_end in runs:
                    # A run that now covers ground an earlier round already
                    # published is split around it, so a repair derives the
                    # missing buckets and neither less nor more.
                    pieces = exclude_planned_windows(
                        candidates=[{"start": run_start, "end": run_end}],
                        planned=[{"start": start, "end": end} for start, end in sorted(published)])
                    for piece in pieces:
                        if len(planned) >= budget:
                            break
                        identity = (f"derive:{recipe_document['recipe_id']}:"
                                    f"{piece['start']}:{piece['end']}")
                        if identity in existing or any(item["dedupe_key"] == identity
                                                       for item in planned):
                            continue
                        step = self._derive_step(task=task, definition=definition,
                                                 execution_id=execution_id,
                                                 recipe_document=recipe_document,
                                                 run_start=piece["start"], run_end=piece["end"],
                                                 identity=identity)
                        if step is not None:
                            planned.append(step)
        return planned, deferred, not_ready

    def _derive_runs(self, *, definition: dict, recipe_document: dict,
                     window_start: str, window_end: str) -> tuple[list[tuple[str, str]],
                                                                  list[tuple[str, str]]] | None:
        """Split one window into ready bucket runs and the buckets that are not ready.

        The readiness rule is the executor's own: a bucket is derivable only when
        every session-open source stamp of that bucket is present in the input the
        step was accepted against.  ``None`` means the input cannot be resolved at
        all right now, which is a deferral rather than a decision about a bucket.
        """
        try:
            snapshot = Catalog(self.canonical_root).resolve(
                recipe_document["input_dataset"],
                {"provider": definition["provider"], "symbol": definition["symbol"],
                 "timeframe": recipe_document["source_timeframe"]})
        except (PublicationError, ValueError):
            return None
        if not snapshot.parts:
            return None
        source_timeframe = recipe_document["source_timeframe"]
        target_timeframe = recipe_document["target_timeframe"]
        source_width = TIMEFRAMES.get(source_timeframe)
        target_width = TIMEFRAMES.get(target_timeframe)
        bounds = _bucket_window(window_start, window_end, target_timeframe)
        if source_width is None or target_width is None or bounds is None:
            return None
        aligned_start = _as_utc(bounds[0], "window_start")
        aligned_end = _as_utc(bounds[1], "window_end")
        selector = {"provider": definition["provider"], "symbol": definition["symbol"],
                    "timeframe": source_timeframe}
        if recipe_document.get("input_recipe_id"):
            selector["recipe_id"] = recipe_document["input_recipe_id"]
        if recipe_document.get("input_recipe_version"):
            selector["recipe_version"] = recipe_document["input_recipe_version"]
        try:
            rows = current_rows(snapshot=snapshot, selector=selector, start=aligned_start,
                                end=aligned_end, source_timeframe=source_timeframe)
        except (OSError, PublicationError, ValueError):
            return None
        if not rows:
            return None
        session = resolve_session_profile(
            recipe_document.get("session_profile") or "instrument",
            provider=definition["provider"], symbol=definition["symbol"], allow_unregistered=True)
        observed: dict[datetime, set[datetime]] = {}
        for row in rows:
            if session.is_open(row.bar_ts):
                observed.setdefault(_bucket_start(row.bar_ts, target_width), set()).add(row.bar_ts)
        runs: list[tuple[str, str]] = []
        blocked: list[tuple[str, str]] = []
        cursor = aligned_start
        while cursor < aligned_end:
            bucket_end = cursor + target_width
            expected = set()
            stamp = cursor
            while stamp < bucket_end:
                if session.is_open(stamp):
                    expected.add(stamp)
                stamp += source_width
            ready = bool(expected) and expected <= observed.get(cursor, set())
            target = runs if ready else blocked
            if target and target[-1][1] == cursor.isoformat():
                target[-1] = (target[-1][0], bucket_end.isoformat())
            else:
                target.append((cursor.isoformat(), bucket_end.isoformat()))
            cursor = bucket_end
        if len(runs) + len(blocked) > DERIVE_RUNS_PER_WINDOW:
            # A pathological window is not expanded without bound: the remainder
            # is deferred and reconsidered on the next tick with the same rules.
            keep = runs[:DERIVE_RUNS_PER_WINDOW]
            blocked = blocked + runs[DERIVE_RUNS_PER_WINDOW:]
            runs = keep
        return runs, blocked

    def _derive_step(self, *, task: dict, definition: dict, execution_id: str, recipe_document: dict,
                     run_start: str, run_end: str, identity: str) -> dict | None:
        """Build one derive step with its fixed input for one ready bucket run."""
        try:
            snapshot = Catalog(self.canonical_root).resolve(
                recipe_document["input_dataset"],
                {"provider": definition["provider"], "symbol": definition["symbol"],
                 "timeframe": recipe_document["source_timeframe"]})
        except (PublicationError, ValueError):
            return None
        if not snapshot.parts:
            return None
        input_id = self.ledger.store_production_input(
            snapshot_reference(self.canonical_root, snapshot))
        job = DeriveJob(
            job_id=f"{task['task_id']}:{execution_id[:8]}:{recipe_document['target_timeframe']}:"
                   f"{run_start[:16].replace(':', '')}",
            provider=definition["provider"], symbol=definition["symbol"],
            recipe_id=recipe_document["recipe_id"], recipe_version=recipe_document["recipe_version"],
            start=run_start, end=run_end, run_scope="production")
        payload = {**job.model_dump(mode="json"),
                   "input_snapshot_id": snapshot.snapshot_id, "input_id": input_id}
        return {"stage": f"derive:{recipe_document['target_timeframe']}",
                "window_start": run_start, "window_end": run_end,
                "dedupe_key": identity, "recipe_id": recipe_document["recipe_id"],
                "timeframe": recipe_document["target_timeframe"], "payloads": [payload]}

    def plan_derived(self, *, task: dict, definition: dict, execution: dict,
                     step_budget: int = 8) -> dict:
        """Plan derive steps for raw windows that are already published.

        A downstream window is planned only when its upstream input exists: the
        recipe chain is walked in dependency order, and an output whose input
        snapshot is still empty is left for a later tick instead of being
        submitted against nothing (spec 6.2).  Every planned step carries the
        fixed input it was accepted against (spec 6.3).
        """
        targets = definition.get("bar_timeframes") or []
        if not targets or self.canonical_root is None:
            return {"steps": [], "deferred": [], "reason": "no_derived_outputs"}
        current = self.ledger.refresh_execution_steps(execution["execution_id"])
        published = [step for step in current if step["stage"] == "raw" and step["state"] == "completed"]
        steps, deferred, not_ready = self._plan_derived_windows(
            task=task, definition=definition, execution_id=execution["execution_id"],
            windows=[(step["window_start"], step["window_end"]) for step in published],
            step_budget=step_budget, skip_completed=True)
        return {"steps": steps, "deferred": deferred, "not_ready": not_ready}

    def retry(self, *, execution_id: str, actor: str | None = None, request_id: str | None = None,
              idempotency_key: str | None = None, step_budget: int = 8,
              now: datetime | None = None) -> dict:
        """Create a linked follow-up round for the needs the original left open.

        A retry re-plans only the windows that did not complete, keeps the
        original round's terminal receipt untouched, and inherits every pause
        and ownership constraint: a paused plan is refused rather than quietly
        producing (spec 5.2, AC13).
        """
        now = now or datetime.now(timezone.utc)
        original = self.ledger.get_production_execution(execution_id)
        if original is None:
            # A refused retry is audited like any other refusal.
            self.ledger.record_write_audit({
                "action": "production.execution.retry", "actor": actor, "request_id": request_id,
                "outcome": "rejected", "code": "not_found",
                "message": "production execution not found"})
            raise KeyError(execution_id)
        task = self._resolve(original["task_id"])
        definition = task.get("payload") or {}
        request = {"execution_id": execution_id, "task_id": task["task_id"]}
        audit = {"action": "production.execution.retry", "actor": actor, "request_id": request_id}

        def action(conn):
            # Every check lives here so that a replay of an already applied
            # retry returns its stored response instead of tripping over the
            # round the first attempt created.
            if original["state"] not in {"completed", "failed", "skipped"}:
                raise ProductionConflict(
                    "active_execution",
                    "the round has not finished yet; retry applies to a terminal round")
            if task["desired_state"] == "paused":
                raise ProductionConflict("task_paused", "resume the plan before retrying its round")
            if task["desired_state"] == "archived":
                raise ProductionConflict("task_archived", "an archived plan cannot be retried")
            if self.ledger.active_execution_for_task(task["task_id"]) is not None:
                raise ProductionConflict("active_execution", "another round of this plan is still active")
            incomplete = [step for step in self.ledger.list_production_steps(execution_id)
                          if step["state"] in {"failed", "blocked", "skipped", "pending", "running"}]
            if not incomplete:
                raise ProductionConflict("nothing_to_retry", "the round has no unfinished work")
            new_execution_id = str(uuid4())
            steps = self._retry_steps(task=task, definition=definition, execution_id=new_execution_id,
                                      steps=incomplete, step_budget=step_budget)
            if not steps:
                raise ProductionConflict("input_unavailable",
                                         "the unfinished windows cannot be replanned yet")
            self.ledger.create_production_execution(
                execution_id=new_execution_id, task_id=task["task_id"],
                definition_version=task["definition_version"], trigger_source="retry",
                conn=conn, retry_of_execution_id=execution_id)
            accepted = self.ledger.accept_execution_plan(
                execution_id=new_execution_id, steps=steps, conn=conn, audit=audit)
            return {"execution_id": new_execution_id, "retry_of_execution_id": execution_id,
                    "task_id": task["task_id"], "planned_steps": len(steps),
                    "step_ids": accepted["step_ids"], "run_ids": accepted["run_ids"]}

        return self._apply(task["task_id"], "retry", action, request=request,
                           idempotency_key=idempotency_key, actor=actor,
                           audit_action="production.execution.retry", request_id=request_id)

    def _retry_steps(self, *, task: dict, definition: dict, execution_id: str,
                     steps: builtins.list[dict], step_budget: int) -> builtins.list[dict]:
        """Re-plan exactly the unfinished windows, one step per original step."""
        planned: list[dict] = []
        deferred: list[str] = []
        for step in steps:
            if len(planned) >= max(1, step_budget):
                break
            window_start, window_end = step.get("window_start"), step.get("window_end")
            if not window_start or not window_end:
                continue
            if step["stage"] == "raw":
                built = self._raw_step(task=task, definition=definition, execution_id=execution_id,
                                       window_start=window_start, window_end=window_end)
            else:
                timeframe = step.get("timeframe") or step["stage"].split(":", 1)[-1]
                try:
                    chain = _recipe_chain(timeframe, raw_timeframe=definition.get("raw_timeframe")
                                          or DEFAULT_RAW_TIMEFRAME,
                                          provider=definition["provider"],
                                          price_basis=definition.get("price_basis") or "bid")
                except DefinitionError:
                    chain = None
                recipe_document = next((item for item in chain or []
                                        if item["target_timeframe"] == timeframe), None)
                if recipe_document is None:
                    continue
                identity = (f"derive:{recipe_document['recipe_id']}:"
                            f"{_as_utc(window_start, 'window_start').isoformat()}:"
                            f"{_as_utc(window_end, 'window_end').isoformat()}")
                built = self._derive_step(task=task, definition=definition, execution_id=execution_id,
                                          recipe_document=recipe_document, window_start=window_start,
                                          window_end=window_end, identity=identity, deferred=deferred)
            if built is not None:
                planned.append(built)
        return planned

    def _raw_step(self, *, task: dict, definition: dict, execution_id: str,
                  window_start: str, window_end: str) -> dict | None:
        """Rebuild one raw window as a step, with the policy in force now."""
        policy = maintenance_policy_for(definition["provider"], definition["raw_timeframe"])
        capability = REGISTRY.capability(definition["provider"])
        job = IngestJob(
            job_id=f"{task['task_id']}:{execution_id[:8]}:raw",
            dataset_id=RAW_DATASET, provider=definition["provider"], symbol=definition["symbol"],
            asset_class=definition.get("asset_class") or capability.asset_classes[0],
            timeframe=definition["raw_timeframe"],
            start=_as_utc(window_start, "window_start"), end=_as_utc(window_end, "window_end"),
            run_scope="production", run_kind="ingest")
        payloads = ingest_window_payloads(job=job, policy=policy)
        if not payloads:
            return None
        payload = payloads[0]
        identity = (f"raw:{_as_utc(payload['start'], 'window_start').isoformat()}:"
                    f"{_as_utc(payload['end'], 'window_end').isoformat()}")
        return {"stage": "raw", "window_start": payload["start"], "window_end": payload["end"],
                "dedupe_key": identity, "payloads": [payload]}

    def catalog_matrix(self) -> dict:
        """Which registered outputs are planned, unplanned, unavailable or drifting.

        The matrix is a read-only projection over the registry and the plan
        registry; it never widens production scope by itself (spec 3.3, AC23).
        """
        plans = self.ledger.list_production_tasks()
        held: dict[str, dict] = {}
        for plan in plans:
            for ownership in self.ledger.ownership_of(plan["task_id"]):
                if ownership["state"] in {"enabled", "paused"}:
                    held[ownership["ownership_key"]] = plan
        rows: list[dict] = []
        for capability in REGISTRY.capabilities():
            instruments = REGISTRY.instruments(capability.provider)
            price_bases = capability.price_bases or ("bid",)
            for instrument in instruments:
                for timeframe in capability.maintenance_timeframes or capability.timeframes:
                    rows.extend(self._matrix_rows(
                        dataset_id=RAW_DATASET, provider=capability.provider,
                        symbol=instrument.symbol, timeframe=timeframe, price_bases=price_bases,
                        held=held))
                for recipe in sorted(REGISTRY.recipes(), key=lambda item: (item.recipe_id, item.version)):
                    if recipe.output_dataset != DERIVED_DATASET:
                        continue
                    rows.extend(self._matrix_rows(
                        dataset_id=DERIVED_DATASET, provider=capability.provider,
                        symbol=instrument.symbol, timeframe=recipe.target_timeframe,
                        price_bases=price_bases, held=held,
                        unavailable_reason=self._recipe_unavailable_reason(
                            recipe, capability.provider, capability.timeframes,
                            capability.maintenance_timeframes or capability.timeframes)))
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return {"rows": rows, "counts": counts, "planned_scope": len(plans),
                "note": "Read-only projection: a matrix row never creates or widens a plan."}

    @staticmethod
    def _recipe_unavailable_reason(recipe, provider: str, timeframes, maintenance_timeframes) -> str | None:
        """Why a registered recipe cannot be planned for this provider, if it cannot."""
        if recipe.allowed_providers and provider not in recipe.allowed_providers:
            return "recipe is not registered for this provider"
        if recipe.source_timeframe not in maintenance_timeframes and recipe.input_dataset == RAW_DATASET:
            return f"{provider} does not maintain {recipe.source_timeframe}"
        if recipe.input_dataset == DERIVED_DATASET and recipe.source_timeframe not in timeframes:
            return f"{recipe.source_timeframe} is not produced for {provider}"
        return None

    @staticmethod
    def _matrix_rows(*, dataset_id: str, provider: str, symbol: str, timeframe: str,
                     price_bases, held: dict, unavailable_reason: str | None = None) -> builtins.list[dict]:
        rows = []
        for price_basis in price_bases:
            key = ownership_key(dataset_id=dataset_id, provider=provider, symbol=symbol,
                                timeframe=timeframe, price_basis=price_basis)
            holder = held.get(key)
            if unavailable_reason is not None:
                status = "unavailable"
            elif holder is None:
                status = "unplanned"
            elif holder.get("health") == "config_drift":
                status = "config_drift"
            else:
                status = "planned"
            rows.append({"dataset_id": dataset_id, "provider": provider, "symbol": symbol,
                         "timeframe": timeframe, "price_basis": price_basis, "ownership_key": key,
                         "status": status, "task_id": None if holder is None else holder["task_id"],
                         "plan_state": None if holder is None else holder["desired_state"],
                         "reason": unavailable_reason})
        return rows

    # -- dispatch and closure -------------------------------------------
    def _raw_coverage(self, *, task: dict, definition: dict, now: datetime):
        """Evaluate published raw coverage over the plan's short scan window.

        Coverage is what decides tail rechecks and interior gaps; the catalog
        read is bounded to the scan window on purpose, and a catalog that cannot
        be read is reported as "no coverage" instead of being treated as empty
        data (which would plan the whole tail again).
        """
        if self.canonical_root is None:
            return None
        scan = coverage_scan_window(definition=definition, now=now)
        if scan is None:
            return None
        capability = REGISTRY.capability(definition["provider"])
        job = _raw_job(task=task, definition=definition, execution={"execution_id": "coverage"},
                       start=scan[0], end=scan[1], run_kind="ingest", capability=capability)
        try:
            return coverage_from_catalog(root=self.canonical_root, job=job)
        except (PublicationError, OSError, ValueError):
            return None

    def _gap_cooldown_windows(self, *, task: dict, definition: dict,
                              now: datetime) -> list[dict]:
        """Gap windows whose terminal failure is still inside the retry cooldown.

        The rule is the maintenance runner's, applied to this plan's own
        terminal runs, so a permanently omitted bar is retried on the governed
        cadence instead of every tick (spec 5.5, AC12).
        """
        policy = maintenance_policy_for(definition["provider"], definition["raw_timeframe"])
        if policy.gap_retry_cooldown_minutes <= 0:
            return []
        since = now - timedelta(minutes=policy.gap_retry_cooldown_minutes)
        runs = self.ledger.recent_plan_runs(task["task_id"], since=since)
        keys = recent_gap_windows(runs=runs, provider=definition["provider"],
                                  symbol=definition["symbol"], run_scope="production",
                                  now=now,
                                  cooldown_minutes=policy.gap_retry_cooldown_minutes)
        return [{"start": start, "end": end} for start, end in sorted(keys)]

    @staticmethod
    def _merge_gaps(previous: list[dict], current: list[dict], *, now: datetime) -> list[dict]:
        """Carry each gap's history forward and bound the plan's gap debt."""
        seen = {(item["window_start"], item["window_end"]): item for item in previous}
        merged: list[dict] = []
        for item in current:
            key = (item["window_start"], item["window_end"])
            carried = seen.get(key) or {}
            attempts = int(carried.get("attempts") or 0) + (1 if item["state"] == "planned" else 0)
            merged.append({**item, "reason": "gap_repair", "attempts": attempts,
                           "first_seen_at": carried.get("first_seen_at") or now.isoformat()})
        merged.sort(key=lambda item: item["window_start"])
        return merged[:GAP_LIMIT]

    def dispatch(self, *, task: dict, execution: dict, now: datetime | None = None,
                 step_budget: int = 8) -> dict:
        """Plan one accepted execution and persist its steps, runs and jobs together."""
        now = now or datetime.now(timezone.utc)
        definition = task.get("payload") or {}
        progress = self.ledger.production_progress(task["task_id"]) or {}
        # Windows whose run ended terminally are owed regardless of how far back
        # they are: the short coverage scan cannot see them, so the ledger does.
        # Step state is read back from the runs first, or a round that just
        # failed would still look pending and its gap would be forgotten.
        self.ledger.refresh_task_steps(task["task_id"])
        carried = self.ledger.outstanding_gap_windows(task["task_id"], limit=GAP_LIMIT)
        coverage = self._raw_coverage(task=task, definition=definition, now=now)
        plan = plan_execution(task, definition, execution, now=now, step_budget=step_budget,
                              progress=progress, coverage=coverage,
                              cooldown_windows=self._gap_cooldown_windows(
                                  task=task, definition=definition, now=now),
                              carried_gaps=carried)
        accepted = self.ledger.accept_execution_plan(
            execution_id=execution["execution_id"], steps=plan["steps"],
            audit={"action": "production.execution.accept", "actor": "system:scheduler",
                   "request_id": None})
        self.ledger.record_progress(task["task_id"], {
            "frontier": plan["frontier"], "effective_end": plan["effective_end"],
            "backlog": plan["backlog"], "last_execution_id": execution["execution_id"],
            "last_planned_at": now.isoformat(), "policy_id": plan.get("policy_id"),
            "planned_windows": len(plan["steps"]),
            "observed_boundary": plan.get("observed_boundary"),
            "complete_boundary": plan.get("complete_boundary"),
            # The high-water mark is what makes a later rewind visible.
            "last_frontier": max([_as_utc(value, "frontier")
                                  for value in (progress.get("last_frontier"), plan["frontier"])
                                  if value is not None]).isoformat(),
            "gaps": self._merge_gaps(progress.get("gaps") or [], plan["gaps"], now=now),
        })
        return {**accepted, "plan": {key: value for key, value in plan.items() if key != "steps"},
                "planned_steps": len(plan["steps"])}

    def reconcile_config_digest(self, *, limit: int = 50) -> dict:
        """Compare each enabled plan with the registry facts it was resolved against.

        A difference means recipe, instrument or policy metadata changed outside
        the plan; the plan stops dispatching and waits for an explicit
        acknowledgement instead of quietly producing under new semantics.
        """
        drifted, healthy = [], []
        for task in self.ledger.list_production_tasks():
            if task["desired_state"] != "enabled" or task["deleted_at"]:
                continue
            if len(drifted) + len(healthy) >= max(1, limit):
                break
            definition = task.get("payload") or {}
            try:
                facts = config_facts(normalize_definition(definition, now=datetime.now(timezone.utc)))
            except (DefinitionError, ValueError):
                # A definition the registry no longer accepts is drift by
                # definition: it cannot be produced as written any more.
                self.ledger.set_task_health(task["task_id"], "config_drift")
                drifted.append(task["task_id"])
                continue
            digest = config_digest(facts)
            stored = self.ledger.config_digest_for(task["task_id"], task["definition_version"])
            if stored is not None and stored != digest:
                self.ledger.set_task_health(task["task_id"], "config_drift")
                drifted.append(task["task_id"])
            else:
                if task.get("health") == "config_drift":
                    healthy.append(task["task_id"])
        return {"config_drift": drifted, "config_ok": healthy}

    def reconcile(self, *, now: datetime | None = None, limit: int = 50) -> dict:
        """Close finished executions and advance the schedules their outcome decides."""
        now = now or datetime.now(timezone.utc)
        drift = self.reconcile_config_digest(limit=limit)
        closure = self._advance_dependency_closure(limit=limit, step_budget=8)
        # A round that died after publishing, or raw published outside a round,
        # is still owed its derived outputs (spec 6.2, AC10).
        publications = self.reconcile_publications(limit=limit, step_budget=8)
        closed = self.ledger.close_finished_executions(limit=limit)
        advanced = []
        for execution in closed:
            task = self.ledger.get_production_task(execution["task_id"])
            if task is None:
                continue
            definition = task.get("payload") or {}
            schedule = definition.get("schedule") or {}
            finished_at = _optional_utc(execution.get("finished_at"))
            if schedule.get("schedule") == "fixed_delay" and finished_at is not None:
                # A fixed-delay plan can only be advanced by a terminal round.
                following = finished_at + timedelta(seconds=int(schedule.get("interval_seconds") or 0))
                self.ledger.set_task_next_run_at(task_id=task["task_id"],
                                                 next_run_at=following.isoformat())
                advanced.append({"task_id": task["task_id"], "next_run_at": following.isoformat()})
            self.ledger.record_progress(task["task_id"], {
                "last_outcome": execution.get("outcome"),
                "last_finished_at": execution.get("finished_at"),
                "last_execution_id": execution["execution_id"],
            })
        return {"closed": closed, "advanced": advanced, "config_drift": drift["config_drift"],
                "derived_planned": closure["planned"] + publications["planned"],
                "derived_deferred": closure["deferred"] + publications["deferred"],
                "reconciled_at": now.isoformat()}

    def _record_repaired_windows(self, *, task: dict, steps: builtins.list[dict]) -> None:
        """Send raw windows that were republished after derivation back for recompute."""
        for step in steps:
            if step["stage"] != "raw" or step["state"] != "completed":
                continue
            if not step.get("window_start") or not step.get("window_end"):
                continue
            if self.ledger.stale_derived_steps(task["task_id"], window_start=step["window_start"],
                                               window_end=step["window_end"],
                                               after_created_at=step["created_at"]):
                self.record_recompute(task["task_id"], window_start=step["window_start"],
                                      window_end=step["window_end"],
                                      reason="raw_republished_after_derivation")

    def _advance_dependency_closure(self, *, limit: int, step_budget: int) -> dict:
        """Plan downstream steps for rounds whose upstream publication is done."""
        planned, deferred = [], []
        if self.canonical_root is None:
            return {"planned": planned, "deferred": deferred}
        for execution in self.ledger.list_running_executions(limit=limit):
            task = self.ledger.get_production_task(execution["task_id"])
            if task is None or task["desired_state"] != "enabled":
                continue
            definition = task.get("payload") or {}
            if not definition.get("bar_timeframes"):
                continue
            # Read the current run outcomes first: the next layer may only be
            # planned once the previous one is genuinely finished.
            steps = self.ledger.refresh_execution_steps(execution["execution_id"])
            if any(step["state"] not in self.ledger.STEP_TERMINAL_STATES for step in steps):
                # Finish the current layer before planning the next one.
                continue
            self._record_repaired_windows(task=task, steps=steps)
            plan = self.plan_derived(task=task, definition=definition, execution=execution,
                                     step_budget=step_budget)
            if plan["steps"]:
                self.ledger.accept_execution_plan(execution_id=execution["execution_id"],
                                                  steps=plan["steps"])
            planned.extend(step["dedupe_key"] for step in plan["steps"])
            deferred.extend(plan["deferred"])
            # Buckets whose input is not complete yet stay visible on the plan
            # instead of disappearing into a log line (spec 5.5, AC12).
            self.ledger.record_progress(task["task_id"], {
                "deferred_derived": [{"step": item, "reason": "input_not_ready"}
                                     for item in sorted(set(plan["not_ready"]))[:GAP_LIMIT]]})
        return {"planned": planned, "deferred": deferred}

    def recompute_pending(self, task_id: str) -> builtins.list[dict]:
        """The persisted recompute set of a plan, for the read model."""
        return self._recompute_ranges(task_id)

    def _run_now(self, conn, task_id: str, *, now: datetime) -> dict:
        """Trigger one manual execution, or locate the execution already in flight."""
        task = self.ledger.get_production_task(task_id)
        if task is None or task["deleted_at"] is not None:
            raise KeyError(task_id)
        if task["desired_state"] == "paused":
            raise ProductionConflict("task_paused", "resume the plan before triggering it")
        if task["desired_state"] == "archived":
            raise ProductionConflict("task_archived", "an archived plan cannot be triggered")
        active = self.ledger.active_execution(conn, task_id)
        if active is not None:
            return {**active, "created": False}
        return self.ledger.create_production_execution(
            execution_id=str(uuid4()), task_id=task_id,
            definition_version=task["definition_version"], trigger_source="manual", conn=conn)


def _default_cursor(definition: dict) -> str | None:
    """Where derivation starts when a plan has never been reconciled."""
    window = definition.get("window_policy") or {}
    return window.get("history_start") or window.get("start")


def _bucket_window(start: str, end: str, timeframe: str) -> tuple[str, str] | None:
    """Clip a window to whole buckets of the target timeframe, or ``None``.

    ``None`` means the window does not yet contain one complete bucket, so the
    step waits for the next tick instead of being submitted with a partial input.
    """
    width = TIMEFRAMES.get(timeframe)
    if width is None or width <= timedelta(0):
        return None
    start_at, end_at = _as_utc(start, "window_start"), _as_utc(end, "window_end")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    seconds = width.total_seconds()
    aligned_start = epoch + timedelta(seconds=((start_at - epoch).total_seconds() // seconds) * seconds)
    aligned_end = epoch + timedelta(seconds=((end_at - epoch).total_seconds() // seconds) * seconds)
    if aligned_end <= aligned_start:
        return None
    return aligned_start.isoformat(), aligned_end.isoformat()


def _bucket_start(stamp: datetime, width: timedelta) -> datetime:
    """The start of the target bucket a source timestamp belongs to."""
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    seconds = width.total_seconds()
    return epoch + timedelta(seconds=((stamp - epoch).total_seconds() // seconds) * seconds)


def scheduled_end(now: datetime, *, lag_minutes: int) -> datetime:
    """The half-open end of data the provider is expected to expose by ``now``."""
    bounded = _as_utc(now, "now") - timedelta(minutes=max(0, lag_minutes))
    return bounded.replace(second=0, microsecond=0)


def coverage_scan_window(*, definition: dict, now: datetime) -> tuple[datetime, datetime] | None:
    """The range a plan's coverage scan covers, or ``None`` for fixed windows.

    The scan is deliberately short: gap debt older than the scan is carried in
    the persisted progress instead of being re-derived from the catalog on every
    tick (spec 5.5, AC12).
    """
    window_policy = definition.get("window_policy") or {}
    if window_policy.get("mode", "continuous") == "fixed":
        return None
    policy = maintenance_policy_for(definition["provider"], definition["raw_timeframe"])
    end = scheduled_end(now, lag_minutes=policy.closed_bar_lag_minutes)
    history_start = _as_utc(window_policy["history_start"], "window_policy.history_start")
    scan_start = max(history_start, end - timedelta(days=policy.tail_days + COVERAGE_SCAN_MARGIN_DAYS))
    return None if scan_start >= end else (scan_start, end)


def _raw_job(*, task: dict, definition: dict, execution: dict, start: datetime, end: datetime,
             run_kind: str, capability) -> IngestJob:
    return IngestJob(
        job_id=f"{task['task_id']}:{execution['execution_id'][:8]}:raw:{run_kind}",
        dataset_id=RAW_DATASET, provider=definition["provider"], symbol=definition["symbol"],
        asset_class=definition.get("asset_class") or capability.asset_classes[0],
        timeframe=definition["raw_timeframe"], start=start, end=end,
        run_scope="production", run_kind=run_kind)


def plan_execution(task: dict, definition: dict, execution: dict, *, now: datetime,
                   step_budget: int = 8, progress: dict | None = None, coverage=None,
                   cooldown_windows: Iterable[dict] = (), carried_gaps: Iterable[dict] = ()) -> dict:
    """Plan the raw windows one execution may expand, bounded by policy and budget.

    Windows are planned in the order the spec requires (5.5): the observed tail
    first, then interior gaps whose governed retry cooldown has expired, then the
    persistent catch-up backlog.  A multi-year backlog therefore stays a
    persisted backlog instead of becoming one unbounded transaction, and the head
    of that backlog can never starve freshness.  A permanently omitted bar is
    retried on the cooldown rather than on every tick, and it is remembered in
    the plan's progress so that advancing the observed maximum can never retire
    it silently.
    """
    now = _as_utc(now, "now")
    window_policy = definition.get("window_policy") or {}
    mode = window_policy.get("mode", "continuous")
    policy = maintenance_policy_for(definition["provider"], definition["raw_timeframe"])
    capability = REGISTRY.capability(definition["provider"])
    bounded_days = min(policy.max_window_days, capability.max_window_days)
    cadence = timeframe_delta(definition["raw_timeframe"])
    budget = max(1, step_budget)
    steps: list[dict] = []
    planned_windows: list[dict] = []
    truncated = 0

    def expand(reason: str, run_kind: str,
               ranges: list[tuple[datetime, datetime]]) -> tuple[list[dict], list[dict]]:
        """Shard candidate ranges through the shared planner, honoring the budget."""
        nonlocal truncated
        payloads: list[dict] = []
        for range_start, range_end in ranges:
            if range_end <= range_start:
                continue
            job = _raw_job(task=task, definition=definition, execution=execution,
                           start=range_start, end=range_end, run_kind=run_kind,
                           capability=capability)
            payloads.extend(ingest_window_payloads(job=job, policy=policy, reason=reason))
        candidates = exclude_planned_windows(
            candidates=[{"start": str(payload["start"]), "end": str(payload["end"]),
                         "payload": payload} for payload in payloads],
            planned=planned_windows)
        planned_here: list[dict] = []
        deferred_here: list[dict] = []
        for candidate in candidates:
            if len(steps) >= budget:
                truncated += 1
                deferred_here.append({"start": candidate["start"], "end": candidate["end"]})
                continue
            planned_windows.append({"start": candidate["start"], "end": candidate["end"]})
            planned_here.append({"start": candidate["start"], "end": candidate["end"]})
            steps.append({"stage": "raw", "window_start": candidate["start"],
                          "window_end": candidate["end"],
                          "dedupe_key": f"raw:{candidate['start']}:{candidate['end']}",
                          "reason": reason, "payloads": [candidate["payload"]]})
        return planned_here, deferred_here

    gaps: list[dict] = []
    gap_ranges: list[dict] = []
    observed_boundary = complete_boundary = None
    if mode == "fixed":
        start = _as_utc(window_policy["start"], "window_policy.start")
        end = _as_utc(window_policy["end"], "window_policy.end")
        frontier_end = start
        if end > start:
            planned, _ = expand("backfill", "ingest", [(start, end)])
            frontier_end = max([_as_utc(item["end"], "window_end") for item in planned], default=start)
    else:
        history_start = _as_utc(window_policy["history_start"], "window_policy.history_start")
        end = scheduled_end(now, lag_minutes=policy.closed_bar_lag_minutes)
        frontier = _optional_utc((progress or {}).get("frontier"))
        # A frontier below the plan's own high-water mark was moved back on
        # purpose (a repair over an older range is requested that way), so that
        # range is fetched again instead of being second-guessed by coverage.
        high_water = _optional_utc((progress or {}).get("last_frontier"))
        rewound = frontier is not None and high_water is not None and frontier < high_water
        data_start = max(history_start, frontier) if frontier else history_start
        scan = coverage_scan_window(definition=definition, now=now)
        scan_start = scan[0] if scan is not None else None
        if coverage is not None:
            observed_boundary = coverage.max_ts.isoformat() if coverage.max_ts else None
            if coverage.latest_complete_boundary is not None:
                complete_boundary = (coverage.latest_complete_boundary + cadence).isoformat()
        ranges = (missing_ranges(coverage=coverage, start=scan_start, end=end)
                  if coverage is not None and scan_start is not None else [])
        trailing = [item for item in ranges if item["trailing"]]
        interior = [item for item in ranges if not item["trailing"]]
        # 1. The observed tail: provider lag and late-arriving bars come first, so
        #    an unresolved interior gap can never hold back new data.
        if trailing:
            expand("tail", "ingest", [(item["start"], item["end"]) for item in trailing])
        # 2. Interior gaps and the debt of windows whose run ended terminally.
        #    The same rules decide both: a gap inside its governed retry cooldown
        #    is reported, not re-planned, and only the part outside the cooldown
        #    is fetched again.
        cooldown = [{"start": window["start"], "end": window["end"]}
                    for window in cooldown_windows]
        candidates = [
            {"start": item["start"].isoformat(), "end": item["end"].isoformat(),
             "source": "coverage"}
            for item in interior
        ] + [
            {"start": _as_utc(item["window_start"], "window_start").isoformat(),
             "end": _as_utc(item["window_end"], "window_end").isoformat(),
             "source": "failed_run"}
            for item in carried_gaps
        ]
        for candidate in candidates:
            parts = exclude_planned_windows(candidates=[candidate], planned=cooldown)
            if not parts:
                gaps.append({"window_start": candidate["start"], "window_end": candidate["end"],
                             "state": "cooldown", "source": candidate["source"]})
                continue
            gap_ranges.extend(parts)
        planned_gaps, deferred_gaps = expand(
            "gap_repair", "gap_repair",
            [(_as_utc(item["start"], "window_start"), _as_utc(item["end"], "window_end"))
             for item in gap_ranges])
        gaps.extend({"window_start": item["start"], "window_end": item["end"],
                     "state": "planned", "source": "gap_repair"} for item in planned_gaps)
        gaps.extend({"window_start": item["start"], "window_end": item["end"],
                     "state": "deferred", "source": "gap_repair"} for item in deferred_gaps)
        # 3. The persistent catch-up backlog, planned from the recorded frontier.
        #    It stops where coverage takes over: the scheduler does not re-plan a
        #    recent range that the provider has already delivered.  A rewind is
        #    the exception, because that is an explicit request to fetch again.
        backfill_limit = scan_start if (scan_start is not None and coverage is not None
                                        and not rewound) else end
        backfill_end = min(end, data_start + timedelta(days=bounded_days), backfill_limit)
        expand("backfill", "backfill", [(data_start, backfill_end)])
        # The frontier only advances over a range this round actually planned, so
        # a budget-truncated backlog is never recorded as covered.  Every group
        # counts, not just the backlog: a tail window fetched from the recorded
        # frontier is exactly as covered as a backfilled one.
        frontier_end = data_start
        for item in sorted(planned_windows, key=lambda window: window["start"]):
            if _as_utc(item["start"], "window_start") > frontier_end:
                break
            frontier_end = max(frontier_end, _as_utc(item["end"], "window_end"))
        if coverage is not None and scan_start is not None and frontier_end >= scan_start:
            # Caught up: coverage, not the cursor, says how far data is complete.
            if interior:
                frontier_end = max(frontier_end,
                                   min(end, min(item["start"] for item in interior)))
            elif complete_boundary is not None:
                frontier_end = max(frontier_end,
                                   min(end, _as_utc(complete_boundary, "complete_boundary")))
    planned_end = max([_as_utc(step["window_end"], "window_end") for step in steps],
                      default=frontier_end)
    return {"steps": steps, "planned_start": frontier_end.isoformat(),
            "planned_end": planned_end.isoformat(), "frontier": frontier_end.isoformat(),
            "effective_end": end.isoformat(), "backlog": frontier_end < end,
            "rewound": rewound, "truncated_windows": truncated, "gaps": gaps,
            "observed_boundary": observed_boundary, "complete_boundary": complete_boundary,
            "reason": "no_work" if not steps else "planned", "policy_id": policy.policy_id}


def as_dict(service_result: dict) -> dict:
    """Small helper for callers that log a service result without the raw payload."""
    return {key: value for key, value in service_result.items() if key != "payload"}
