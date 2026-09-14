"""Production task module: one interface for plan definitions, preview and change.

The WebUI HTTP adapter and the scheduler process both call into this module;
neither builds job payloads nor re-interprets policy on its own (spec 7.1).
Plan identity, ownership, schedule and version handling live here, while the
ledger stays responsible for durable, transactional state.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from uuid import uuid4

from .platform_registry import REGISTRY, config_digest, maintenance_policy_for
from .runs.ledger import IdempotencyConflict, ProductionConflict, RunLedger
from .scheduler import MIN_INTERVAL_SECONDS, next_run_at, validate_schedule

RAW_DATASET = "provider_bars"
DERIVED_DATASET = "market_bars"
OBSERVATION_DATASET = "economic_observations"
DEFAULT_RAW_TIMEFRAME = "1m"
PREVIEW_RUNS = 5
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

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
            "publication_policy": recipe.publication_policy}


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
                 cursor_ttl_seconds: float = 3600.0):
        self.ledger = ledger
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
        return {
            **task,
            "ownership": self.ledger.ownership_of(task["task_id"]),
            "executions": executions,
            "current_execution": active,
            "health": self._health(task, active),
            "tombstone": bool(task["deleted_at"]),
            "schedule": {"kind": (definition.get("schedule") or {}).get("schedule"),
                         "next_run_at": task.get("next_run_at")},
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
            return self._apply(task_id, command,
                               lambda conn: self.ledger.acknowledge_config_drift(
                                   conn, task_id, expected_version=expected_version),
                               request=request, idempotency_key=idempotency_key, actor=actor,
                               audit_action=audit_action, request_id=request_id)

        raise ProductionConflict("unsupported_command", f"{command} is not available in this phase")

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


def as_dict(service_result: dict) -> dict:
    """Small helper for callers that log a service result without the raw payload."""
    return {key: value for key, value in service_result.items() if key != "payload"}
