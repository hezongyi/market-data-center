"""Unified maintenance task contract for the WebUI data workbench.

The console submits provider ingest, backfill, gap repair, derive, quality and
parity work through one validated request.  Planning is side-effect free so the
UI can preview window counts, coverage and capacity impact before anything is
queued, and every submission is recorded in the write audit trail.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel

from data_center.catalog.registry import iter_dataset_definitions
from data_center.catalog.snapshot import Catalog
from data_center.domain.models import DeriveJob, IngestJob
from data_center.platform import (
    build_ingest_plan,
    coverage_from_catalog,
    enqueue_ingest_plan,
)
from data_center.platform_registry import (
    REGISTRY,
    maintenance_policy_for,
    resolve_capability,
)
from data_center.production_tasks import PLAN_HEALTH, PLAN_PHASES
from data_center.scheduler import MIN_INTERVAL_SECONDS, SUPPORTED_SCHEDULES

PROVIDER_DATASET = "provider_bars"
DERIVED_DATASET = "market_bars"
ECONOMIC_DATASET = "economic_observations"
PAGE_OF_WINDOWS = 20
WINDOW_SEMANTICS = "half-open"
RunKind: TypeAlias = Literal["ingest", "derive", "backfill", "gap_repair", "quality", "parity"]
RunScope: TypeAlias = Literal["production", "acceptance", "migration", "maintenance"]
RUN_SCOPES = ("production", "acceptance", "migration", "maintenance")
# Datasets each run kind may target.  This matrix is the single authority: the
# console reads it to disable options the platform cannot serve, the planner
# rejects anything outside it, and the default dataset per run kind is derived
# from it instead of being restated.
RUN_KIND_DATASETS: dict[str, tuple[str, ...]] = {
    "ingest": (PROVIDER_DATASET, ECONOMIC_DATASET),
    "derive": (DERIVED_DATASET,),
    "backfill": (PROVIDER_DATASET, ECONOMIC_DATASET),
    "gap_repair": (PROVIDER_DATASET,),
    "quality": (PROVIDER_DATASET, ECONOMIC_DATASET),
    "parity": (DERIVED_DATASET,),
}
RUN_KINDS = tuple(RUN_KIND_DATASETS)
# Runs that only verify existing data.  They publish no canonical part, so they
# must never be mistaken for a data-producing run by the worker or the console.
VERIFICATION_RUN_KINDS = ("quality", "parity")
RUN_KIND_DEFAULT_DATASET = {
    run_kind: DERIVED_DATASET if DERIVED_DATASET in datasets else PROVIDER_DATASET
    for run_kind, datasets in RUN_KIND_DATASETS.items()
}


class MaintenanceTaskRequest(BaseModel):
    """One maintenance request, normalized by :func:`evaluate_task`."""

    run_kind: RunKind = "ingest"
    run_scope: RunScope = "production"
    # Deliberately free-form: an unknown dataset must reach the planner, which
    # answers with a stable ``unsupported_dataset`` error, and must not fail
    # inside pydantic (whose raw message is not part of the API contract).
    dataset_id: str | None = None
    provider: str = "fixture"
    symbol: str | None = None
    asset_class: str | None = None
    timeframe: str = "1d"
    series_id: str | None = None
    recipe_id: str | None = None
    recipe_version: str | None = None
    price_basis: str | None = None
    start: datetime
    end: datetime
    task_id: str | None = None
    managed_dataset_id: str | None = None
    schedule: Literal["manual"] = "manual"


class MaintenanceTaskError(ValueError):
    """A request that cannot be planned; carries the field and safe code."""

    def __init__(self, message: str, *, field: str = "request", code: str = "invalid_request"):
        super().__init__(message)
        self.field = field
        self.code = code


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _error(field: str, code: str, message: str) -> dict:
    return {"field": field, "code": code, "message": message}


def resolve_dataset(request: MaintenanceTaskRequest) -> str:
    """Select the dataset a task writes, without guessing beyond the request.

    An explicit ``series_id`` means the caller asked for the economic dataset;
    whether the requested run kind can actually serve it is decided by
    :data:`RUN_KIND_DATASETS`, so the two rules cannot drift apart.
    """
    if request.dataset_id:
        return request.dataset_id
    if request.series_id:
        return ECONOMIC_DATASET
    return RUN_KIND_DEFAULT_DATASET.get(request.run_kind, PROVIDER_DATASET)


def _validate_run_kind_dataset(run_kind: str, dataset_id: str) -> list[dict]:
    """Reject combinations the platform cannot execute.

    ``RUN_KIND_DATASETS`` is also exposed through ``/capabilities``; keeping
    the same matrix at the planner boundary prevents callers from bypassing
    the UI and enqueueing a job whose worker semantics do not match its
    declared run kind.
    """
    allowed = RUN_KIND_DATASETS.get(run_kind, ())
    if dataset_id not in allowed:
        return [_error(
            "run_kind",
            "unsupported_run_kind",
            f"{run_kind} is not available for {dataset_id}; supported datasets: {', '.join(allowed)}",
        )]
    return []


def _resolve_asset_class(request: MaintenanceTaskRequest, provider: str) -> str | None:
    if request.asset_class:
        return request.asset_class
    if request.symbol:
        try:
            return REGISTRY.instrument(provider, request.symbol).asset_class
        except ValueError:
            pass
    try:
        capability = REGISTRY.capability(provider)
    except ValueError:
        return "test" if request.run_scope == "acceptance" else None
    if len(capability.asset_classes) == 1 and "*" not in capability.asset_classes:
        return capability.asset_classes[0]
    return "test" if request.run_scope == "acceptance" else None


def _selector(task: dict) -> dict:
    keys = ("provider", "symbol", "timeframe", "recipe_id", "recipe_version", "price_basis", "series_id")
    return {key: task[key] for key in keys if task.get(key)}


def _task_document(request: MaintenanceTaskRequest, dataset_id: str, *, asset_class: str | None,
                   run_kind: str) -> dict:
    task_id = request.task_id or f"{run_kind}-{request.provider}-{request.symbol or request.series_id or 'dataset'}"
    return {
        "task_id": task_id,
        "managed_dataset_id": request.managed_dataset_id,
        "run_kind": run_kind,
        "run_scope": request.run_scope,
        "dataset_id": dataset_id,
        "provider": request.provider,
        "symbol": request.symbol,
        "asset_class": asset_class,
        "timeframe": request.timeframe,
        "series_id": request.series_id,
        "recipe_id": request.recipe_id,
        "recipe_version": request.recipe_version,
        "price_basis": request.price_basis,
        "start": _iso(request.start),
        "end": _iso(request.end),
        "time_range": {"start": _iso(request.start), "end": _iso(request.end), "semantics": WINDOW_SEMANTICS},
        "schedule": "manual",
    }


def _capability_document(provider: str, *, allow_unregistered: bool) -> dict | None:
    try:
        capability = resolve_capability(provider, allow_unregistered=allow_unregistered)
    except ValueError:
        return None
    return {
        "provider": capability.provider,
        "asset_classes": list(capability.asset_classes),
        "timeframes": list(capability.timeframes),
        "maintenance_timeframes": list(capability.maintenance_timeframes or capability.timeframes),
        "price_bases": list(capability.price_bases),
        "max_window_days": capability.max_window_days,
        "session_profile": capability.session_profile,
    }


def _recipe_document(recipe) -> dict:
    return {
        "recipe_id": recipe.recipe_id,
        "recipe_version": recipe.version,
        "input_dataset": recipe.input_dataset,
        "output_dataset": recipe.output_dataset,
        "source_timeframe": recipe.source_timeframe,
        "target_timeframe": recipe.target_timeframe,
        "providers": list(recipe.allowed_providers),
        "price_bases": list(recipe.allowed_price_bases),
        "session_profile": recipe.session_profile,
        "materialization": recipe.materialization,
        "partial_bucket_policy": recipe.partial_bucket_policy,
        "missing_input_policy": recipe.missing_input_policy,
    }


def _plan_id(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _requested_days(request: MaintenanceTaskRequest) -> int:
    return max(0, math.ceil((request.end - request.start).total_seconds() / 86_400))


def _policy_for(provider: str, timeframe: str):
    try:
        return maintenance_policy_for(provider, timeframe)
    except ValueError:
        return None


def _ingest_job(task: dict, request: MaintenanceTaskRequest) -> IngestJob:
    return IngestJob(
        job_id=task["task_id"], managed_dataset_id=request.managed_dataset_id,
        dataset_id=task["dataset_id"], provider=request.provider,
        symbol=request.symbol or "", asset_class=task["asset_class"] or "crypto",
        timeframe=request.timeframe, start=request.start, end=request.end,
        run_scope=request.run_scope, run_kind=request.run_kind,
    )


def _derived_context(task: dict, request: MaintenanceTaskRequest, root: Path) -> dict:
    """Resolve the recipe and immutable input snapshot for derived work."""
    if not request.recipe_id or not request.recipe_version:
        raise MaintenanceTaskError("recipe_id and recipe_version are required for derived work",
                                   field="recipe_id", code="recipe_required")
    try:
        recipe = REGISTRY.recipe(request.recipe_id, request.recipe_version)
    except ValueError as exc:
        raise MaintenanceTaskError(str(exc), field="recipe_id", code="recipe_not_registered") from exc
    if recipe.output_dataset != task["dataset_id"]:
        raise MaintenanceTaskError(
            f"recipe {recipe.recipe_id}@{recipe.version} produces {recipe.output_dataset}, "
            f"not {task['dataset_id']}", field="recipe_id", code="recipe_dataset_mismatch")
    price_bases = tuple(recipe.allowed_price_bases)
    if not price_bases:
        price_bases = resolve_capability(request.provider,
                                         allow_unregistered=request.run_scope == "acceptance").price_bases
    price_basis = request.price_basis or (price_bases[0] if len(price_bases) == 1 else None)
    if not price_basis:
        raise MaintenanceTaskError("price_basis is required for this recipe", field="price_basis",
                                   code="price_basis_required")
    if price_bases and "*" not in price_bases and price_basis not in price_bases:
        raise MaintenanceTaskError(f"unsupported price basis for {recipe.recipe_id}: {price_basis}",
                                   field="price_basis", code="unsupported_price_basis")
    snapshot = Catalog(root).resolve(recipe.input_dataset, {
        "provider": request.provider, "symbol": request.symbol or "", "timeframe": recipe.source_timeframe,
    })
    return {"recipe": recipe, "price_basis": price_basis, "snapshot": snapshot}


def evaluate_task(*, request: MaintenanceTaskRequest, root: Path, capacity_policy=None) -> dict:
    """Validate one maintenance request and describe exactly what it would do.

    Nothing is queued here: the preview is the same computation the submission
    performs, so a caller can never be surprised by a different plan.  The live
    capacity policy is injected by the API; the default is used for pure
    validation callers.
    """
    from data_center.capacity import CapacityPolicy

    policy = capacity_policy or CapacityPolicy()
    errors: list[dict] = []
    warnings: list[dict] = []
    dataset_id = resolve_dataset(request)
    asset_class = _resolve_asset_class(request, request.provider)
    task = _task_document(request, dataset_id, asset_class=asset_class, run_kind=request.run_kind)
    errors.extend(_validate_run_kind_dataset(request.run_kind, dataset_id))
    if request.end <= request.start:
        errors.append(_error("end", "invalid_time_range", "end must be after start"))
    if dataset_id == ECONOMIC_DATASET:
        if not request.series_id:
            errors.append(_error("series_id", "series_required", "series_id is required for economic work"))
        if request.provider != "fred":
            errors.append(_error("provider", "unsupported_provider",
                                 "economic_observations is served by the fred provider"))
    elif dataset_id in {PROVIDER_DATASET, DERIVED_DATASET}:
        if not request.symbol:
            errors.append(_error("symbol", "symbol_required", "symbol is required for market data work"))
        if dataset_id == PROVIDER_DATASET and not asset_class:
            errors.append(_error("asset_class", "asset_class_required",
                                 f"asset_class cannot be inferred for {request.provider}; select one explicitly"))
    else:
        errors.append(_error("dataset_id", "unsupported_dataset", f"unsupported dataset: {dataset_id}"))

    capability = None
    if dataset_id in {PROVIDER_DATASET, DERIVED_DATASET} and request.provider:
        capability = _capability_document(request.provider, allow_unregistered=request.run_scope == "acceptance")
        if capability is None:
            errors.append(_error("provider", "unsupported_provider",
                                 f"provider capability is not registered: {request.provider}"))

    plan: dict | None = None
    coverage: dict | None = None
    snapshot: dict | None = None
    recipe_document: dict | None = None
    if not errors and dataset_id == PROVIDER_DATASET:
        job = _ingest_job(task, request)
        coverage_result = None
        if request.run_kind == "gap_repair":
            coverage_result = coverage_from_catalog(root=root, job=job)
            coverage = coverage_result.as_dict()
        maintenance_policy = _policy_for(request.provider, request.timeframe)
        try:
            built = build_ingest_plan(job=job, coverage=coverage_result, policy=maintenance_policy)
        except ValueError as exc:
            errors.append(_error("request", "plan_rejected", str(exc)))
            built = None
        if built is not None:
            windows = built["windows"]
            if request.run_kind == "gap_repair" and not windows:
                errors.append(_error("run_kind", "no_gap_detected",
                                     "coverage is ready for this range; there is no gap to repair"))
            plan = {
                "plan_id": _plan_id(built), "reason": request.run_kind,
                "window_count": len(windows), "semantics": WINDOW_SEMANTICS,
                "windows": windows[:PAGE_OF_WINDOWS], "truncated": len(windows) > PAGE_OF_WINDOWS,
                "session_profile": built.get("session_profile"),
                "config_digest": built.get("config_digest"),
            }
    elif not errors and dataset_id == DERIVED_DATASET:
        try:
            context = _derived_context(task, request, root)
        except MaintenanceTaskError as exc:
            errors.append(_error(exc.field, exc.code, str(exc)))
        except ValueError as exc:
            errors.append(_error("selector", "input_snapshot_error", str(exc)))
        else:
            recipe = context["recipe"]
            task["price_basis"] = context["price_basis"]
            recipe_document = _recipe_document(recipe)
            snapshot = {"input_snapshot_id": context["snapshot"].snapshot_id,
                        "dataset_id": recipe.input_dataset,
                        "part_count": len(context["snapshot"].parts),
                        "schema_versions": list(getattr(context["snapshot"], "schema_versions", ()) or ())}
            if not context["snapshot"].parts:
                errors.append(_error("symbol", "input_snapshot_empty",
                                     f"no {recipe.input_dataset} parts match the selector"))
            plan = {"plan_id": _plan_id({"recipe": recipe.recipe_id, "version": recipe.version,
                                         "selector": _selector(task), "start": _iso(request.start),
                                         "end": _iso(request.end)}),
                    "reason": request.run_kind, "window_count": 1, "semantics": WINDOW_SEMANTICS,
                    "windows": [{"ordinal": 0, "start": _iso(request.start), "end": _iso(request.end),
                                 "reason": request.run_kind, "semantics": WINDOW_SEMANTICS}],
                    "truncated": False}
            if request.run_kind == "parity" and not errors:
                warnings.append({"code": "parity_read_only",
                                 "message": "parity verification reads derived and raw data and publishes nothing"})
    elif not errors and dataset_id == ECONOMIC_DATASET:
        plan = {"plan_id": _plan_id({"dataset": ECONOMIC_DATASET, "series_id": request.series_id,
                                     "start": _iso(request.start), "end": _iso(request.end)}),
                "reason": request.run_kind, "window_count": 1, "semantics": WINDOW_SEMANTICS,
                "windows": [{"ordinal": 0, "start": _iso(request.start), "end": _iso(request.end),
                             "reason": request.run_kind, "semantics": WINDOW_SEMANTICS}],
                "truncated": False}

    if request.run_kind in VERIFICATION_RUN_KINDS and not errors:
        warnings.append({"code": "verification_run",
                         "message": f"{request.run_kind} runs record findings and publish no canonical part"})
    if request.run_scope == "acceptance":
        warnings.append({"code": "acceptance_scope",
                         "message": "acceptance runs stay outside the production run scope"})

    capacity = capacity_document(request, plan, errors=errors, policy=policy, root=root)
    status = "invalid" if errors else "valid"
    return {"task": task, "validation": {"status": status, "errors": errors, "warnings": warnings},
            "plan": plan, "coverage": coverage, "snapshot": snapshot, "recipe": recipe_document,
            "capability": capability, "capacity": capacity,
            "submittable": status == "valid" and capacity["write_status"] == "available",
            "write_status": capacity["write_status"], "generated_at": datetime.now(timezone.utc).isoformat()}


def capacity_document(request: MaintenanceTaskRequest, plan: dict | None, *, errors: list[dict],
                      policy, root: Path) -> dict:
    """Describe the write protection a submission would meet, without raising."""
    snapshot = policy.inspect(root)
    requested_days = _requested_days(request)
    protected_reason = None
    if snapshot.status == "critical":
        protected_reason = {"code": "capacity_critical",
                            "message": "capacity is critical; new maintenance work is disabled"}
    elif request.run_kind == "backfill" and snapshot.status == "warning" and requested_days > 31:
        protected_reason = {"code": "capacity_warning_backfill",
                            "message": "capacity is in warning state; unattended backfill over 31 days is disabled"}
    write_status = "protected" if protected_reason else "available"
    return {
        "status": snapshot.status, "free_ratio": snapshot.free_ratio,
        "warning_free_ratio": snapshot.warning_free_ratio, "critical_free_ratio": snapshot.critical_free_ratio,
        "requested_days": requested_days, "policy": "backfill" if request.run_kind == "backfill" else "ingest",
        "estimated_windows": (plan or {}).get("window_count", 0),
        "write_status": write_status, "protected_reason": protected_reason,
        "blocked_by_validation": bool(errors),
    }


def evaluate_with_capacity(*, request: MaintenanceTaskRequest, root: Path, capacity_policy) -> dict:
    """Preview a task with the live capacity policy injected from settings."""
    return evaluate_task(request=request, root=root, capacity_policy=capacity_policy)


def queued_envelope(*, task: dict, run_ids: list[str], plan_id: str | None, input_snapshot_id: str | None,
                    capacity: dict, submitted_at: str, audit_id: int | None = None,
                    warnings: list[dict] | None = None) -> dict:
    """The single envelope every write endpoint returns for queued work."""
    return {
        "status": "queued", "state": "queued",
        "task_id": task["task_id"], "job_id": task["task_id"],
        "dataset_id": task["dataset_id"], "run_kind": task["run_kind"], "run_scope": task["run_scope"],
        "provider": task.get("provider"), "symbol": task.get("symbol"),
        "selector": _selector(task), "time_range": task["time_range"],
        "run_id": run_ids[0] if run_ids else None, "run_ids": run_ids,
        "window_count": len(run_ids),
        "plan_id": plan_id, "input_snapshot_id": input_snapshot_id,
        "capacity": capacity, "warnings": warnings or [],
        "submitted_at": submitted_at, "audit_id": audit_id,
    }


def submit_task(*, request: MaintenanceTaskRequest, ledger, root: Path, capacity_policy,
                request_id: str | None = None, actor: str | None = None) -> dict:
    """Validate, protect, enqueue and audit one maintenance task."""
    preview = evaluate_with_capacity(request=request, root=root, capacity_policy=capacity_policy)
    task = preview["task"]
    errors = preview["validation"]["errors"]
    if errors:
        first = errors[0]
        raise MaintenanceTaskError(first["message"], field=first["field"], code=first["code"])

    # Reuse the governed capacity gate rather than re-deriving protection: the
    # preview must never disagree with what actually blocks a write.
    requested_days = preview["capacity"]["requested_days"]
    if request.run_kind == "backfill":
        capacity_policy.require_backfill_capacity(root, requested_days=requested_days)
    else:
        capacity_policy.require_ingest_capacity(root)

    dataset_id = task["dataset_id"]
    plan_id = (preview["plan"] or {}).get("plan_id")
    input_snapshot_id = None
    if dataset_id == PROVIDER_DATASET:
        job = _ingest_job(task, request)
        coverage_result = None
        if request.run_kind == "gap_repair":
            coverage_result = coverage_from_catalog(root=root, job=job)
        run_ids = enqueue_ingest_plan(ledger=ledger, job=job, coverage=coverage_result,
                                      policy=_policy_for(request.provider, request.timeframe),
                                      request_id=request_id)
    elif dataset_id == ECONOMIC_DATASET:
        run_ids = [ledger.enqueue_job({
            "job_id": task["task_id"], "dataset_id": ECONOMIC_DATASET, "provider": "fred",
            "series_id": request.series_id,
            # The economic dataset is daily; the provider expects plain dates.
            "start": request.start.date().isoformat(), "end": request.end.date().isoformat(),
            "run_scope": request.run_scope, "run_kind": request.run_kind,
            "schema_version": "economic_observations.v2", "request_id": request_id,
        })]
    elif dataset_id == DERIVED_DATASET:
        context = _derived_context(task, request, root)
        input_snapshot_id = context["snapshot"].snapshot_id
        if request.run_kind == "parity":
            # A parity check verifies the derived layer and publishes nothing;
            # it must never be submitted as a derive job.
            run_ids = [ledger.enqueue_job({
                "job_id": task["task_id"], "dataset_id": DERIVED_DATASET, "provider": request.provider,
                "symbol": request.symbol, "timeframe": context["recipe"].target_timeframe,
                "price_basis": context["price_basis"], "recipe_id": request.recipe_id,
                "recipe_version": request.recipe_version, "input_snapshot_id": input_snapshot_id,
                "start": _iso(request.start), "end": _iso(request.end),
                "run_kind": "parity", "run_scope": request.run_scope, "request_id": request_id,
            })]
        else:
            from data_center.ingest.worker import LocalWorker

            job = DeriveJob(
                job_id=task["task_id"], provider=request.provider, symbol=request.symbol or "",
                recipe_id=request.recipe_id or "", recipe_version=request.recipe_version or "",
                start=request.start, end=request.end,
                input_snapshot_id=input_snapshot_id, run_scope=request.run_scope,
            )
            run_ids = [LocalWorker(root, ledger).submit_derive(job)]
    else:  # pragma: no cover - evaluate_task rejects unknown datasets
        raise MaintenanceTaskError(f"unsupported dataset: {dataset_id}", code="unsupported_dataset")

    submitted_at = datetime.now(timezone.utc).isoformat()
    ledger.upsert_maintenance_task(task["task_id"], {**task, "run_ids": run_ids, "submitted_at": submitted_at}, "queued")
    audit = ledger.record_write_audit({
        "action": f"maintenance.{request.run_kind}", "actor": actor, "request_id": request_id,
        "task_id": task["task_id"], "run_ids": run_ids, "run_kind": task["run_kind"],
        "run_scope": task["run_scope"], "dataset_id": dataset_id, "selector": _selector(task),
        "time_range": task["time_range"], "outcome": "queued", "code": None,
        "message": f"{len(run_ids)} run(s) queued",
    })
    return queued_envelope(task=task, run_ids=run_ids, plan_id=plan_id, input_snapshot_id=input_snapshot_id,
                           capacity=preview["capacity"], submitted_at=submitted_at,
                           audit_id=audit["audit_id"], warnings=preview["validation"]["warnings"])


def task_summary(preview: dict) -> dict[str, Any]:
    """Compact projection used by the console's task list."""
    return {"task": preview["task"], "validation": preview["validation"], "capacity": preview["capacity"],
            "plan": preview.get("plan"), "submittable": preview["submittable"]}


def platform_capabilities(capacity_policy, config, ledger) -> dict:
    """Describe what the platform can actually do, for form validation.

    The console disables an option only because this read model says it is
    unavailable, never because the browser guessed.
    """
    providers = []
    for capability in REGISTRY.capabilities():
        if not config.provider_allowed(capability.provider):
            continue
        instruments = [{
            "provider": item.provider, "symbol": item.symbol, "canonical_symbol": item.canonical_symbol,
            "asset_class": item.asset_class, "currency": item.currency,
            "session_profile": item.session_profile, "calendar_profile": item.calendar_profile,
            "approved": item.approved,
        } for item in REGISTRY.instruments(capability.provider)
          if config.preview_symbol_allowed(item.symbol)]
        providers.append({
            "provider": capability.provider,
            "asset_classes": list(capability.asset_classes),
            "timeframes": list(capability.timeframes),
            "maintenance_timeframes": list(capability.maintenance_timeframes or capability.timeframes),
            "price_bases": list(capability.price_bases),
            "max_window_days": capability.max_window_days,
            "session_profile": capability.session_profile,
            "instruments": instruments,
        })
    recipes = [_recipe_document(recipe) for recipe in REGISTRY.recipes()]
    capacity = capacity_policy.inspect(config.canonical_root)
    datasets = []
    for definition in iter_dataset_definitions():
        entry = definition.as_dict()
        entry["kind"] = definition.kind
        entry["quality_profile"] = definition.quality_profile
        entry["query_modes"] = list(definition.query_modes)
        entry["retention_policy"] = definition.retention_policy
        entry["materialization_policy"] = definition.materialization_policy
        datasets.append(entry)
    production = {
        # A form disables an option only because this read model says it is
        # unavailable; the browser never guesses (spec 8, AC01).
        "schedule_kinds": sorted(SUPPORTED_SCHEDULES),
        "minimum_interval_seconds": MIN_INTERVAL_SECONDS,
        "plan_states": ["enabled", "paused", "archived"],
        # The enums come from the module that produces them, so a value the read
        # model can return is always one the form is allowed to offer (spec 8).
        "plan_health": sorted(PLAN_HEALTH),
        "plan_phases": sorted(PLAN_PHASES),
        "block_reasons": ["provider_backoff", "capacity", "dependency", "backlog", "input_unavailable",
                          "provider_gap", "paused", "global_pause", "execution_in_progress", "config_drift"],
        "outputs": [{"dataset_id": "provider_bars", "timeframes": sorted({
            timeframe for item in providers for timeframe in item["maintenance_timeframes"]})},
            {"dataset_id": "market_bars", "timeframes": sorted({
                recipe["target_timeframe"] for recipe in recipes})}],
        "scheduler_enabled": ledger.dispatch_enabled(),
    }
    return {
        "datasets": sorted(datasets, key=lambda item: item["dataset_id"]),
        "providers": providers,
        "production": production,
        "recipes": sorted(recipes, key=lambda item: (item["recipe_id"], item["recipe_version"])),
        "run_kinds": [{"run_kind": kind, "datasets": RUN_KIND_DATASETS[kind]} for kind in RUN_KINDS],
        "economic_series_provider": "fred",
        "write_status": "protected" if capacity.status == "critical" else "available",
        "capacity": capacity.as_dict(),
        "maintenance_policies": [
            {"policy_id": policy.policy_id, "max_window_days": policy.max_window_days,
             "tail_days": policy.tail_days, "shard_days": policy.shard_days,
             "shard_minutes": policy.shard_minutes, "closed_bar_lag_minutes": policy.closed_bar_lag_minutes}
            for policy in REGISTRY.maintenance_policies()
        ],
    }
