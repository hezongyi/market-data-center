"""Read-only verification runs: quality checks and derived-versus-raw parity.

A verification run never publishes a canonical part.  It reports findings so the
console can separate "the data is wrong" from "the job failed", and the worker
persists those findings additively without touching any run receipt.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from data_center.catalog.snapshot import Catalog
from data_center.control_plane import evaluate_coverage, timeframe_delta
from data_center.domain.models import IngestJob
from data_center.platform_registry import REGISTRY, resolve_capability
from data_center.quality.checks import check_economic_observations, check_provider_bars
from data_center.storage.query import (
    query_economic_observations,
    query_market_bars,
    query_provider_bars,
)

VERIFICATION_RUN_KINDS = ("quality", "parity")


def _finding(finding: dict, *, severity: str | None = None) -> dict:
    record = dict(finding)
    if severity is not None:
        record.setdefault("severity", severity)
    record.setdefault("message", record.get("code", "quality finding"))
    return record


def _coverage_findings(coverage, *, severity: str) -> list[dict]:
    if coverage.readiness_status == "ready":
        return []
    code = "coverage_not_ready" if coverage.readiness_status == "not_ready" else "coverage_degraded"
    return [_finding({
        "code": code, "severity": severity,
        "message": (f"coverage {coverage.readiness_status}: {coverage.gap_count} of "
                    f"{coverage.expected_timestamp_count} expected timestamps are missing"),
        "coverage": coverage.as_dict(),
    })]


def run_quality_verification(*, job: dict, root: Path, run_id: str) -> dict:
    """Check one selector/time range and record findings; publish nothing."""
    dataset_id = job.get("dataset_id", "provider_bars")
    started_at = datetime.now(timezone.utc).isoformat()
    if dataset_id == "economic_observations":
        return _quality_economic(job=job, root=root, run_id=run_id, started_at=started_at)
    return _quality_provider_bars(job=job, root=root, run_id=run_id, started_at=started_at)


def _quality_provider_bars(*, job: dict, root: Path, run_id: str, started_at: str) -> dict:
    from data_center.connectors.registry import get_connector

    ingest_job = IngestJob.model_validate(job)
    connector = get_connector(ingest_job.provider)
    rows = connector.fetch_bars(ingest_job)
    findings = [_finding(item, severity="error") for item in check_provider_bars(rows)]
    window_rows = [row for row in rows if ingest_job.start <= row.bar_ts < ingest_job.end]
    capability = resolve_capability(ingest_job.provider, allow_unregistered=ingest_job.run_scope == "acceptance")
    try:
        session_id = REGISTRY.instrument(ingest_job.provider, ingest_job.symbol).session_profile
    except ValueError:
        session_id = capability.session_profile
    coverage = evaluate_coverage(
        dataset_id=dataset_id_for(ingest_job), selector={"provider": ingest_job.provider,
                                                         "symbol": ingest_job.symbol,
                                                         "timeframe": ingest_job.timeframe},
        rows=(row.model_dump() for row in window_rows), timeframe=timeframe_delta(ingest_job.timeframe),
        quality_status="pass" if not findings else "fail",
        session_profile=REGISTRY.session(session_id),
        requested_start=ingest_job.start, requested_end=ingest_job.end,
    )
    # A gap is recorded as a warning: it explains a degraded dataset without
    # claiming the verification itself failed to run.
    findings.extend(_coverage_findings(coverage, severity="warning"))
    return _verification_receipt(job=job, run_id=run_id, findings=findings, coverage=coverage.as_dict(),
                                 row_count=len(rows), started_at=started_at)


def _quality_economic(*, job: dict, root: Path, run_id: str, started_at: str) -> dict:
    from data_center.ingest.economic import default_fred_connector

    series_id = job.get("series_id")
    if not series_id:
        raise ValueError("series_id is required for an economic quality check")
    connector = default_fred_connector()
    rows = connector.fetch_observations(series_id, start=job.get("start"), end=job.get("end"))
    findings = [_finding(item, severity="error") for item in check_economic_observations(
        [{**row, "series_id": series_id} for row in rows])]
    coverage = query_economic_observations(root, provider="fred", series_id=series_id,
                                           start=job.get("start"), end=job.get("end"))
    if not rows:
        findings.append(_finding({"code": "no_observations", "severity": "warning",
                                  "message": "the provider returned no observations for this range"}))
    return _verification_receipt(job=job, run_id=run_id, findings=findings,
                                 coverage={"dataset_id": "economic_observations", "provider": "fred",
                                           "series_id": series_id,
                                           "row_count": len(coverage),
                                           "min_date": coverage[0]["observation_date"] if coverage else None,
                                           "max_date": coverage[-1]["observation_date"] if coverage else None},
                                 row_count=len(rows), started_at=started_at)


def run_parity_verification(*, job: dict, root: Path, run_id: str) -> dict:
    """Verify stored derived bars against the raw layer that produced them."""
    started_at = datetime.now(timezone.utc).isoformat()
    recipe_id = job.get("recipe_id")
    recipe_version = job.get("recipe_version")
    if not recipe_id or not recipe_version:
        raise ValueError("recipe_id and recipe_version are required for a parity check")
    recipe = REGISTRY.recipe(recipe_id, recipe_version)
    provider = job["provider"]
    symbol = job["symbol"]
    price_basis = job.get("price_basis") or (recipe.allowed_price_bases[0] if recipe.allowed_price_bases else "raw")
    start = datetime.fromisoformat(str(job["start"]))
    end = datetime.fromisoformat(str(job["end"]))
    stored = query_market_bars(root, provider=provider, symbol=symbol, timeframe=recipe.target_timeframe,
                               price_basis=price_basis, recipe_id=recipe_id, recipe_version=recipe_version,
                               start=start, end=end)
    findings: list[dict] = []
    if not stored:
        findings.append(_finding({"code": "parity_missing_outputs", "severity": "error",
                                  "message": "no derived rows exist for this selector and range"}))
    snapshot = Catalog(root).resolve(recipe.input_dataset, {
        "provider": provider, "symbol": symbol, "timeframe": recipe.source_timeframe,
    })
    drifted = [row for row in stored if row.get("input_snapshot_id") != snapshot.snapshot_id]
    if drifted:
        findings.append(_finding({
            "code": "lineage_drift", "severity": "error",
            "message": (f"{len(drifted)} derived row(s) reference an input snapshot that is no longer "
                        f"current ({snapshot.snapshot_id[:12]}…)"),
        }))
    raw_rows = query_provider_bars(root, provider=provider, symbol=symbol,
                                   timeframe=recipe.source_timeframe, start=start, end=end)
    capability = resolve_capability(provider, allow_unregistered=job.get("run_scope") == "acceptance")
    try:
        session_id = REGISTRY.instrument(provider, symbol).session_profile
    except ValueError:
        session_id = capability.session_profile
    source_coverage = evaluate_coverage(
        dataset_id=recipe.input_dataset,
        selector={"provider": provider, "symbol": symbol, "timeframe": recipe.source_timeframe},
        rows=raw_rows, timeframe=timeframe_delta(recipe.source_timeframe), quality_status="pass",
        session_profile=REGISTRY.session(session_id), requested_start=start, requested_end=end,
    )
    derived_coverage = evaluate_coverage(
        dataset_id=recipe.output_dataset,
        selector={"provider": provider, "symbol": symbol, "timeframe": recipe.target_timeframe},
        rows=stored, timeframe=timeframe_delta(recipe.target_timeframe), quality_status="pass",
        session_profile=REGISTRY.session(session_id), requested_start=start, requested_end=end,
        calendar_unit="month" if recipe.target_timeframe == "1mo" else
        "week" if recipe.target_timeframe == "1w" else "fixed",
    )
    findings.extend(_coverage_findings(derived_coverage, severity="error"))
    if source_coverage.readiness_status != "ready" and derived_coverage.readiness_status == "ready":
        findings.append(_finding({
            "code": "source_degraded", "severity": "warning",
            "message": "derived coverage is complete while the raw source still reports gaps",
        }))
    errors = [item for item in findings if item.get("severity") == "error"]
    return _verification_receipt(job=job, run_id=run_id, findings=findings,
                                 coverage={"derived": derived_coverage.as_dict(),
                                           "source": source_coverage.as_dict()},
                                 row_count=len(stored), started_at=started_at,
                                 status="fail" if errors else "pass",
                                 extras={"input_snapshot_id": snapshot.snapshot_id,
                                         "checked_row_count": len(stored), "recipe_id": recipe_id,
                                         "recipe_version": recipe_version, "price_basis": price_basis})


def dataset_id_for(job: IngestJob) -> str:
    return job.dataset_id


def _verification_receipt(*, job: dict, run_id: str, findings: list[dict], coverage, row_count: int,
                          started_at: str, status: str | None = None, extras: dict | None = None) -> dict:
    errors = [item for item in findings if item.get("severity") == "error"]
    resolved_status = status or ("fail" if errors else "pass")
    return {
        "run_id": run_id, "job_id": job.get("job_id"), "dataset_id": job.get("dataset_id"),
        "run_kind": job.get("run_kind", "quality"), "run_scope": job.get("run_scope", "production"),
        "provider": job.get("provider"), "symbol": job.get("symbol"), "timeframe": job.get("timeframe"),
        "series_id": job.get("series_id"), "recipe_id": job.get("recipe_id"),
        "recipe_version": job.get("recipe_version"),
        "status": resolved_status, "row_count": row_count,
        "verification": {"kind": job.get("run_kind", "quality"), "checked_at": started_at,
                         "publishes_parts": False},
        "quality_summary": {"status": "pass" if not errors else "fail",
                            "finding_count": len(findings), "findings": findings},
        "coverage": coverage,
        "started_at": started_at,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **(extras or {}),
    }
