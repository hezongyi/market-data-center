"""Stable market-data platform interface used by schedulers and acceptance."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

from data_center.catalog.registry import get_dataset_definition
from data_center.control_plane import (
    CoverageResult,
    IngestWindow,
    MaintenancePolicy,
    SessionProfile,
    evaluate_coverage,
    immutable_execution_plan,
    timeframe_delta,
)
from data_center.control_plane import (
    plan_maintenance as _plan_maintenance,
)
from data_center.domain.models import IngestJob
from data_center.ingest.service import run_fixture_ingest
from data_center.platform_registry import REGISTRY, config_digest, resolve_capability
from data_center.storage.query import query_provider_bars


def _metadata_value(metadata, field: str):
    return getattr(metadata, field) if hasattr(metadata, field) else metadata.get(field)


def build_ingest_plan(*, job: IngestJob, coverage: CoverageResult | None = None,
                      policy: MaintenancePolicy | None = None) -> dict:
    definition = get_dataset_definition(job.dataset_id)
    capability = resolve_capability(job.provider, allow_unregistered=job.run_scope == "acceptance")
    if "*" not in capability.asset_classes and job.asset_class not in capability.asset_classes:
        raise ValueError(f"unsupported asset class for {job.provider}: {job.asset_class}")
    if "*" not in capability.timeframes and job.timeframe not in capability.timeframes:
        raise ValueError(f"unsupported timeframe for {job.provider}: {job.timeframe}")
    maintenance_timeframes = capability.maintenance_timeframes or capability.timeframes
    if "*" not in maintenance_timeframes and job.timeframe not in maintenance_timeframes:
        raise ValueError(f"unsupported maintenance timeframe for {job.provider}: {job.timeframe}")
    try:
        instrument = REGISTRY.instrument(job.provider, job.symbol)
        if not instrument.approved:
            raise ValueError(f"instrument is not approved: {job.provider}/{job.symbol}")
        if instrument.asset_class != job.asset_class:
            raise ValueError(
                f"instrument asset class mismatch for {job.provider}/{job.symbol}: "
                f"expected {instrument.asset_class}, got {job.asset_class}"
            )
        session_profile_id = instrument.session_profile
    except ValueError:
        # Fixture is a controlled contract-test provider and intentionally
        # accepts synthetic symbols.  External providers require an approved
        # instrument manifest outside acceptance runs.
        if job.run_scope != "acceptance" and job.provider != "fixture":
            raise
        instrument = {"provider": job.provider, "symbol": job.symbol,
                      "asset_class": job.asset_class, "currency": "USD",
                      "session_profile": capability.session_profile,
                      "calendar_profile": capability.session_profile}
        session_profile_id = capability.session_profile
    session_profile = REGISTRY.session(session_profile_id)
    requested_policy = policy or REGISTRY.maintenance_policy()
    effective_policy = requested_policy.model_copy(update={
        "max_window_days": min(requested_policy.max_window_days, capability.max_window_days),
    })
    reason = "gap_repair" if job.run_kind == "gap_repair" else "backfill" if job.run_kind == "backfill" else "ingest"
    windows = _plan_maintenance(start=job.start, end=job.end, coverage=coverage,
                                policy=effective_policy, reason=reason,
                                timeframe=timeframe_delta(job.timeframe))
    digests = {
        "dataset_digest": config_digest(definition),
        "capability_digest": config_digest(capability),
        "instrument_digest": config_digest(instrument),
        "session_profile_digest": config_digest(session_profile),
        "calendar_digest": config_digest({"calendar_profile": _metadata_value(instrument, "calendar_profile")}),
        "quality_profile_digest": config_digest(REGISTRY.quality_profile(definition.quality_profile)),
        "maintenance_policy_digest": config_digest(effective_policy),
    }
    return {**immutable_execution_plan(run_kind=job.run_kind, run_scope=job.run_scope,
                                       dataset_id=job.dataset_id,
                                       selector={"provider": job.provider, "symbol": job.symbol,
                                                 "timeframe": job.timeframe}, windows=windows,
                                       config_digest=config_digest(digests)),
            "config_digests": digests, "session_profile": session_profile_id,
            "calendar_profile": _metadata_value(instrument, "calendar_profile"),
            "price_bases": list(capability.price_bases)}


def ingest_window_payloads(*, job: IngestJob, coverage: CoverageResult | None = None,
                           policy: MaintenancePolicy | None = None,
                           request_id: str | None = None) -> list[dict]:
    """Expand one maintenance request into independently retryable run payloads."""
    plan = build_ingest_plan(job=job, coverage=coverage, policy=policy)
    windows = plan["windows"]
    if not windows:
        # Preserve queue semantics for the legacy zero-width validation
        # request; the worker will reject the provider request safely rather
        # than indexing an empty plan at the API boundary.
        payload = job.model_dump(mode="json")
        payload.update({"request_id": request_id} if request_id is not None else {})
        return [payload]
    plan_id = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    payloads = []
    for window in windows:
        bounded_job = job.model_copy(update={
            "job_id": job.job_id if len(windows) == 1 else f"{job.job_id}:w{window['ordinal']:04d}",
            "start": datetime.fromisoformat(window["start"]),
            "end": datetime.fromisoformat(window["end"]),
        })
        execution_plan = {**plan, "windows": [window], "maintenance_plan_id": plan_id}
        payload = bounded_job.model_dump(mode="json")
        payload.update({"execution_plan": execution_plan, "maintenance_plan_id": plan_id,
                        "window_ordinal": window["ordinal"]})
        if request_id is not None:
            payload["request_id"] = request_id
        payloads.append(payload)
    return payloads


def enqueue_ingest_plan(*, ledger, job: IngestJob, coverage: CoverageResult | None = None,
                        policy: MaintenancePolicy | None = None,
                        request_id: str | None = None) -> list[str]:
    """Queue every planned window as its own run, manifest, and receipt."""
    return [ledger.enqueue_job(payload) for payload in ingest_window_payloads(
        job=job, coverage=coverage, policy=policy, request_id=request_id,
    )]


def plan_maintenance(*, dataset_id: str, selector: Mapping[str, str], start: datetime, end: datetime,
                     run_kind: str = "backfill", run_scope: str = "maintenance",
                     asset_class: str, coverage: CoverageResult | None = None,
                     policy: MaintenancePolicy | None = None) -> dict:
    job = IngestJob(job_id="plan", dataset_id=dataset_id, provider=selector["provider"],
                    symbol=selector["symbol"], timeframe=selector["timeframe"], asset_class=asset_class,
                    start=start, end=end, run_kind=run_kind, run_scope=run_scope)
    return build_ingest_plan(job=job, coverage=coverage, policy=policy)


def coverage_from_catalog(*, root: Path, job: IngestJob) -> CoverageResult:
    """Evaluate governed current-state coverage before planning maintenance.

    This is intentionally provider-agnostic: the only provider-specific work is
    the already registered connector/capability.  The catalog/query layer
    supplies current rows, while the control plane supplies session semantics.
    """
    capability = resolve_capability(job.provider, allow_unregistered=job.run_scope == "acceptance")
    try:
        session_id = REGISTRY.instrument(job.provider, job.symbol).session_profile
    except ValueError:
        if job.run_scope != "acceptance" and job.provider != "fixture":
            raise
        session_id = capability.session_profile
    rows = query_provider_bars(root, provider=job.provider, symbol=job.symbol, timeframe=job.timeframe)
    return coverage_for_rows(
        dataset_id=job.dataset_id,
        selector={"provider": job.provider, "symbol": job.symbol, "timeframe": job.timeframe},
        rows=rows, session_profile=REGISTRY.session(session_id),
        requested_start=job.start, requested_end=job.end,
        timeframe=timeframe_delta(job.timeframe),
    )


def plan_maintenance_from_catalog(*, root: Path, job: IngestJob,
                                  policy: MaintenancePolicy | None = None) -> dict:
    """Return coverage plus a deterministic plan for a scheduled maintenance run."""
    coverage = coverage_from_catalog(root=root, job=job)
    return {"coverage": coverage.as_dict(),
            "plan": build_ingest_plan(job=job, coverage=coverage, policy=policy)}


def execute_ingest(*, window: IngestWindow, job: IngestJob, root: Path, connector=None,
                   ledger=None, run_id: str | None = None, execution_plan: dict | None = None) -> dict:
    """Execute exactly one bounded window and return its governed receipt."""
    if ledger is None and job.run_scope in {"production", "maintenance"}:
        raise ValueError("governed worker/ledger is required for production ingest")
    bounded_job = job.model_copy(update={"start": window.start, "end": window.end})
    plan = execution_plan or build_ingest_plan(job=bounded_job)
    receipt = run_fixture_ingest(bounded_job, root, ledger=ledger, run_id=run_id, connector=connector,
                                 execution_plan=plan)
    return {**receipt, "window": window.as_dict()}


def coverage_for_rows(*, dataset_id: str, selector: Mapping[str, str], rows: list[dict],
                      quality_status: str = "pass", session_profile: SessionProfile | None = None,
                      requested_start: datetime | None = None,
                      requested_end: datetime | None = None,
                      timeframe: timedelta | None = None) -> CoverageResult:
    resolved_timeframe = timeframe or timeframe_delta(selector.get("timeframe", "1m"))
    return evaluate_coverage(dataset_id=dataset_id, selector=selector, rows=rows,
                             timeframe=resolved_timeframe, quality_status=quality_status,
                             session_profile=session_profile,
                             requested_start=requested_start, requested_end=requested_end)
