import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from data_center.catalog.manifest import build_manifest, manifest_path, write_manifest
from data_center.connectors.registry import get_connector
from data_center.control_plane import IngestWindow, evaluate_coverage, timeframe_delta
from data_center.domain.models import IngestJob
from data_center.domain.schema import SCHEMA_VERSION, validate_provider_bars
from data_center.lineage import compact_source_hashes
from data_center.platform_registry import REGISTRY, resolve_capability
from data_center.quality.checks import check_provider_bars
from data_center.quality.errors import QualityError
from data_center.storage.parquet import write_provider_bars


def _execution_windows(job: IngestJob, execution_plan: dict | None) -> list[IngestWindow]:
    """Validate and materialize the immutable windows handed to the worker.

    The planner is deliberately outside the connector.  A worker must not
    silently ignore its plan and issue one unbounded provider request.
    """
    if execution_plan is None:
        return [IngestWindow(start=job.start, end=job.end, reason=job.run_kind, ordinal=0)]
    if execution_plan.get("run_kind") != job.run_kind or execution_plan.get("run_scope") != job.run_scope:
        raise ValueError("execution plan run classification does not match job")
    if execution_plan.get("dataset_id") != job.dataset_id:
        raise ValueError("execution plan dataset does not match job")
    selector = execution_plan.get("selector") or {}
    expected_selector = {"provider": job.provider, "symbol": job.symbol, "timeframe": job.timeframe}
    if any(selector.get(key) != value for key, value in expected_selector.items()):
        raise ValueError("execution plan selector does not match job")
    raw_windows = execution_plan.get("windows")
    if not isinstance(raw_windows, list) or not raw_windows:
        raise ValueError("execution plan must contain at least one window")
    windows = [IngestWindow(
        start=datetime.fromisoformat(item["start"]), end=datetime.fromisoformat(item["end"]),
        reason=item["reason"], ordinal=int(item["ordinal"]),
    ) for item in raw_windows]
    parent_start = job.start.astimezone(timezone.utc)
    parent_end = job.end.astimezone(timezone.utc)
    previous_end = parent_start
    for expected_ordinal, window in enumerate(windows):
        if len(windows) > 1 and window.ordinal != expected_ordinal:
            raise ValueError("execution plan window ordinals must be contiguous")
        if window.start < parent_start or window.end > parent_end or window.start < previous_end:
            raise ValueError("execution plan window is outside job range or overlaps another window")
        previous_end = window.end
    if job.run_kind != "gap_repair" and (windows[0].start != parent_start or windows[-1].end != parent_end):
        raise ValueError("execution plan windows must cover the complete job range")
    return windows


def run_fixture_ingest(job: IngestJob, root: Path, ledger=None, run_id: str | None = None, connector=None,
                       execution_plan: dict | None = None) -> dict:
    resolved_run_id = run_id or str(uuid4())
    connector = connector or get_connector(job.provider)
    rows = []
    window_receipts = []
    if execution_plan is None:
        # Direct service calls remain useful for connector contract tests.  All
        # production worker submissions carry an immutable plan and therefore
        # take the bounded-window path below.
        rows = connector.fetch_bars(job)
    else:
        windows = _execution_windows(job, execution_plan)
        for window in windows:
            bounded_job = job.model_copy(update={"start": window.start, "end": window.end})
            fetched_rows = connector.fetch_bars(bounded_job)
            # Enforce the platform's half-open boundary even for adapters
            # whose legacy client happens to include the right endpoint.
            if getattr(connector, "end_inclusive", False):
                window_rows = [row for row in fetched_rows if window.start <= row.bar_ts <= window.end]
            else:
                window_rows = [row for row in fetched_rows if window.start <= row.bar_ts < window.end]
            if not window_rows:
                raise ValueError(f"provider returned no bars for window {window.ordinal}")
            timestamps = [row.bar_ts for row in window_rows]
            if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
                raise ValueError(f"provider returned unsorted or duplicate timestamps for window {window.ordinal}")
            rows.extend(window_rows)
            window_receipts.append({
                **window.as_dict(), "row_count": len(window_rows),
                "min_ts": min(row.bar_ts for row in window_rows).isoformat(),
                "max_ts": max(row.bar_ts for row in window_rows).isoformat(),
            })
    unique_rows = {}
    for row in rows:
        key = (row.provider, row.symbol, row.timeframe, row.bar_ts, row.price_type)
        existing = unique_rows.get(key)
        if existing is not None and existing.model_dump(exclude={"ingest_ts"}) != row.model_dump(exclude={"ingest_ts"}):
            raise ValueError("execution plan windows produced conflicting duplicate primary keys")
        unique_rows[key] = existing or row
    rows = sorted(unique_rows.values(), key=lambda row: row.bar_ts)
    capability = resolve_capability(job.provider, allow_unregistered=job.run_scope == "acceptance")
    allowed_price_bases = set(capability.price_bases)
    if allowed_price_bases and any(row.price_type not in allowed_price_bases for row in rows):
        raise ValueError("provider returned a price basis outside its registered capability")
    if any((row.provider, row.symbol, row.timeframe) != (job.provider, job.symbol.upper(), job.timeframe)
           for row in rows):
        raise ValueError("provider returned rows outside the requested selector")
    if any(row.price_type != rows[0].price_type for row in rows):
        raise ValueError("provider returned mixed price bases")
    validate_provider_bars(rows)
    findings = check_provider_bars(rows)
    if findings:
        raise QualityError(f"provider quality check failed: {findings[0]['code']}", findings)
    session_id = (execution_plan or {}).get("session_profile", capability.session_profile)
    session = REGISTRY.session(session_id)
    coverage = evaluate_coverage(
        dataset_id=job.dataset_id,
        selector={"provider": job.provider, "symbol": job.symbol, "timeframe": job.timeframe},
        rows=(row.model_dump() for row in rows), timeframe=timeframe_delta(job.timeframe),
        quality_status="pass", session_profile=session,
        requested_start=job.start, requested_end=job.end,
    )
    if coverage.readiness_status != "ready":
        raise QualityError("provider coverage is not ready", [{
            "severity": "error", "code": "coverage_not_ready",
            "coverage": coverage.as_dict(),
        }])
    paths = write_provider_bars(root, rows, part_id=resolved_run_id)
    output_hash = sha256(json.dumps([row.model_dump(mode="json") for row in rows], sort_keys=True).encode()).hexdigest()
    input_hash = sha256(job.model_dump_json().encode()).hexdigest()
    source_lineage = compact_source_hashes(row.source_hash for row in rows)
    payload = {"run_id": resolved_run_id, "job_id": job.job_id, "status": "pass", "dataset_id": job.dataset_id,
               "schema_version": SCHEMA_VERSION, "provider": job.provider,
               "run_kind": job.run_kind, "run_scope": job.run_scope,
               "execution_plan": execution_plan,
               "windows": window_receipts,
               "connector_version": getattr(connector, "version", "1"), "input_hash": input_hash,
               "row_count": len(rows), "min_ts": min(row.bar_ts for row in rows).isoformat(),
               "max_ts": max(row.bar_ts for row in rows).isoformat(), "paths": [str(path) for path in paths],
               "manifest": str(manifest_path(root, resolved_run_id)), "output_hash": output_hash,
               "lineage": {"input_kind": "provider_request", "source_snapshot_id": None,
                           **source_lineage},
               "coverage": coverage.as_dict(),
               "quality_summary": {"status": "pass", "finding_count": 0, "findings": []},
               "created_at": datetime.now(timezone.utc).isoformat()}
    write_manifest(root, build_manifest(
        root, run_id=resolved_run_id, dataset_id=payload["dataset_id"],
        schema_version=payload["schema_version"], paths=paths, row_count=len(rows),
        quality_summary=payload["quality_summary"], lineage=payload["lineage"],
        run_kind=job.run_kind, run_scope=job.run_scope,
        config_digests=(execution_plan or {}).get("config_digests"),
    ))
    if ledger is not None:
        ledger.put(payload["run_id"], payload)
    return payload
