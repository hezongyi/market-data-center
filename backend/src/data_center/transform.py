"""Snapshot-bound, provider-agnostic transform recipe executor."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from uuid import uuid4

import polars as pl

from data_center.catalog.manifest import build_manifest, manifest_path, write_manifest
from data_center.catalog.registry import get_dataset_definition
from data_center.catalog.snapshot import CatalogSnapshot
from data_center.control_plane import (
    TIMEFRAME_DELTAS,
    SessionProfile,
    TransformRecipe,
    evaluate_coverage,
)
from data_center.domain.models import MarketBar, ProviderBar
from data_center.domain.schema import validate_market_bars
from data_center.lineage import compact_source_hashes
from data_center.platform_registry import REGISTRY, config_digest, resolve_capability
from data_center.quality.checks import check_market_bars
from data_center.storage.parquet import write_market_bars

TIMEFRAMES = TIMEFRAME_DELTAS


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _bucket(value: datetime, width: timedelta) -> datetime:
    stamp = value.astimezone(timezone.utc)
    seconds = int(width.total_seconds())
    return datetime.fromtimestamp((int(stamp.timestamp()) // seconds) * seconds, tz=timezone.utc)


def _bucket_for_timeframe(value: datetime, timeframe: str) -> datetime:
    stamp = value.astimezone(timezone.utc)
    if timeframe == "1w":
        return (stamp - timedelta(days=stamp.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
    if timeframe == "1mo":
        return stamp.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return _bucket(stamp, TIMEFRAMES[timeframe])


def _next_bucket(value: datetime, timeframe: str) -> datetime:
    value = value.astimezone(timezone.utc)
    if timeframe == "1w":
        return value + timedelta(days=7)
    if timeframe == "1mo":
        if value.month == 12:
            return value.replace(year=value.year + 1, month=1, day=1)
        return value.replace(month=value.month + 1, day=1)
    return value + TIMEFRAMES[timeframe]


def _resolve_session_profile(recipe: TransformRecipe, *, provider: str, symbol: str,
                             allow_unregistered: bool) -> SessionProfile:
    """Resolve a named session profile without coupling the executor to a provider."""
    # A missing profile is a configuration error.  Falling back to 24x7 can
    # silently manufacture bars across FX weekends, holidays, or exchange
    # closures and therefore cannot be safe for canonical publication.
    if recipe.session_profile == "instrument":
        try:
            return REGISTRY.session(REGISTRY.instrument(provider, symbol).session_profile)
        except ValueError:
            if not allow_unregistered:
                raise
            return REGISTRY.session("utc_24x7")
    return REGISTRY.session(recipe.session_profile)


def _current_rows(*, snapshot: CatalogSnapshot, selector: dict[str, str],
                  start: datetime, end: datetime, source_timeframe: str) -> list[ProviderBar | MarketBar]:
    """Read only the selected source slice, then resolve current-state rows.

    Catalog resolution already narrows a snapshot to immutable parts.  The
    additional path and Parquet predicates are important when a snapshot was
    intentionally resolved at a coarser selector (for example provider-only):
    a derivation must not materialize every symbol, year, or timeframe before
    applying its own selector and time range.
    """
    partition_keys = {"provider", "asset_class", "symbol", "timeframe", "price_basis", "year"}
    path_selector = {key: str(value) for key, value in selector.items() if key in partition_keys}
    path_selector["timeframe"] = source_timeframe
    candidates = [
        part for part in snapshot.parts
        if all(f"{key}={value}" in set(part.path.parts) for key, value in path_selector.items())
    ]
    if not candidates:
        return []

    predicates = [
        pl.col("timeframe") == source_timeframe,
        pl.col("bar_ts") >= pl.lit(start),
        pl.col("bar_ts") < pl.lit(end),
    ]
    row_fields = set(ProviderBar.model_fields) | set(MarketBar.model_fields)
    for key, value in selector.items():
        if key in row_fields:
            predicates.append(pl.col(key) == pl.lit(value))
    predicate = predicates[0]
    for condition in predicates[1:]:
        predicate = predicate & condition
    # scan_parquet keeps the filter below the scan, allowing statistics and
    # row-group pruning.  No full-part DataFrame is created for excluded rows.
    source = pl.concat(
        [pl.scan_parquet(part.path, hive_partitioning=False).filter(predicate) for part in candidates],
        how="vertical",
    ).collect()
    current: dict[tuple, ProviderBar | MarketBar] = {}
    for raw in source.to_dicts():
        row = (ProviderBar.model_validate(raw) if snapshot.dataset_id == "provider_bars"
               else MarketBar.model_validate(raw))
        key = (row.provider, row.symbol, row.timeframe, row.bar_ts,
               getattr(row, "price_type", getattr(row, "price_basis", None)),
               getattr(row, "session_profile", None), getattr(row, "recipe_id", None),
               getattr(row, "recipe_version", None))
        if key not in current or current[key].ingest_ts < row.ingest_ts:
            current[key] = row
    return sorted(current.values(), key=lambda row: (row.bar_ts, row.provider, row.symbol, row.timeframe))


class TransformExecutor:
    """Execute an immutable recipe against one immutable input snapshot."""

    version = "transform-executor.v1"

    def derive(self, *, recipe: TransformRecipe, input_snapshot: CatalogSnapshot,
               selector: dict[str, str], start: datetime, end: datetime, root: Path,
               run_id: str | None = None, run_scope: str = "production") -> dict:
        if input_snapshot.dataset_id != recipe.input_dataset:
            raise ValueError("recipe input dataset does not match snapshot")
        if not set(input_snapshot.schema_versions).issubset(recipe.allowed_schema_versions):
            raise ValueError("recipe does not allow snapshot schema")
        output = get_dataset_definition(recipe.output_dataset)
        if output.kind != "derived":
            raise ValueError("recipe output must be a derived dataset")
        if recipe.materialization != output.materialization_policy:
            raise ValueError("recipe materialization conflicts with dataset definition")
        if recipe.publication_policy != output.publication_policy:
            raise ValueError("recipe publication policy conflicts with dataset definition")
        source_width = TIMEFRAMES.get(recipe.source_timeframe)
        target_width = TIMEFRAMES.get(recipe.target_timeframe)
        if source_width is None or target_width is None or target_width < source_width:
            raise ValueError("unsupported recipe timeframe")
        ratio = target_width.total_seconds() / source_width.total_seconds()
        calendar_ratio = recipe.target_timeframe == "1mo" and recipe.source_timeframe == "1d"
        if not calendar_ratio and int(ratio) != ratio:
            raise ValueError("target timeframe must be an integer multiple of source timeframe")
        start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        source_selector = dict(selector)
        if recipe.input_recipe_id is not None:
            source_selector["recipe_id"] = recipe.input_recipe_id
        if recipe.input_recipe_version is not None:
            source_selector["recipe_version"] = recipe.input_recipe_version
        source = _current_rows(snapshot=input_snapshot, selector=source_selector, start=start, end=end,
                               source_timeframe=recipe.source_timeframe)
        session = _resolve_session_profile(
            recipe, provider=source[0].provider, symbol=source[0].symbol,
            allow_unregistered=run_scope == "acceptance",
        )
        source = [row for row in source if session.is_open(row.bar_ts)]
        if not source:
            raise ValueError("recipe input is empty")
        identities = {(row.provider, row.symbol, row.asset_class, row.currency) for row in source}
        if len(identities) != 1:
            raise ValueError("recipe selector must resolve exactly one instrument")
        source_bases = {getattr(row, "price_type", getattr(row, "price_basis", None)) for row in source}
        if len(source_bases) != 1:
            raise ValueError("recipe selector must resolve exactly one price basis")
        groups: dict[datetime, list[ProviderBar | MarketBar]] = defaultdict(list)
        for row in source:
            groups[_bucket_for_timeframe(row.bar_ts, recipe.target_timeframe)].append(row)
        # Derived row identity must be reproducible from the immutable input
        # snapshot and recipe.  Wall-clock execution time belongs in the run
        # receipt, not in the materialized row hash.
        generated_at = max(row.ingest_ts for row in source)
        derived: list[MarketBar] = []
        for bucket, rows in sorted(groups.items()):
            rows.sort(key=lambda row: row.bar_ts)
            bucket_end = _next_bucket(bucket, recipe.target_timeframe)
            expected_timestamps = set()
            cursor = bucket
            while cursor < bucket_end:
                if session.is_open(cursor):
                    expected_timestamps.add(cursor)
                cursor += source_width
            observed_timestamps = {row.bar_ts for row in rows}
            complete = observed_timestamps == expected_timestamps and all(
                right.bar_ts - left.bar_ts == source_width for left, right in pairwise(rows)
            )
            if not complete:
                if recipe.missing_input_policy == "fail":
                    raise ValueError(f"incomplete input bucket: {bucket.isoformat()}")
                if recipe.partial_bucket_policy == "drop":
                    continue
            first, last = rows[0], rows[-1]
            basis = getattr(first, "price_type", getattr(first, "price_basis", None))
            if any(getattr(row, "price_type", getattr(row, "price_basis", None)) != basis for row in rows):
                raise ValueError("mixed price basis in input bucket")
            derived.append(MarketBar(
                symbol=first.symbol, asset_class=first.asset_class, provider=first.provider,
                timeframe=recipe.target_timeframe, source_timeframe=recipe.source_timeframe,
                bar_ts=bucket, open=first.open, high=max(row.high for row in rows),
                low=min(row.low for row in rows), close=last.close,
                volume=None if any(row.volume is None for row in rows) else sum(row.volume or 0 for row in rows),
                currency=first.currency, price_basis=basis, session_profile=session.profile_id,
                recipe_id=recipe.recipe_id, recipe_version=recipe.version,
                input_snapshot_id=input_snapshot.snapshot_id, ingest_ts=generated_at,
                source_hash=_hash([row.source_hash for row in rows]),
            ))
        if not derived:
            raise ValueError("recipe produced no complete output buckets")
        validate_market_bars(derived)
        findings = check_market_bars(derived)
        if findings:
            raise ValueError(f"derived quality check failed: {findings[0]['code']}")
        input_coverage = evaluate_coverage(
            dataset_id=recipe.input_dataset, selector=source_selector,
            rows=({"bar_ts": row.bar_ts} for row in source), timeframe=source_width,
            quality_status="pass", session_profile=session,
            requested_start=start, requested_end=end,
        )
        output_start = _bucket_for_timeframe(start, recipe.target_timeframe)
        output_end = _next_bucket(_bucket_for_timeframe(end - timedelta(microseconds=1), recipe.target_timeframe),
                                  recipe.target_timeframe)
        output_calendar = {"1w": "week", "1mo": "month"}.get(recipe.target_timeframe, "fixed")
        output_coverage = evaluate_coverage(
            dataset_id=recipe.output_dataset,
            selector={"provider": derived[0].provider, "symbol": derived[0].symbol,
                      "timeframe": recipe.target_timeframe, "price_basis": derived[0].price_basis},
            rows=({"bar_ts": row.bar_ts} for row in derived), timeframe=target_width,
            quality_status="pass", session_profile=session,
            requested_start=output_start, requested_end=output_end, calendar_unit=output_calendar,
        )
        if input_coverage.readiness_status != "ready":
            raise ValueError("recipe input coverage is not ready")
        if output_coverage.readiness_status != "ready":
            raise ValueError("recipe output coverage is not ready")
        resolved_run_id = run_id or uuid4().hex
        provider = derived[0].provider
        symbol = derived[0].symbol
        capability = resolve_capability(provider, allow_unregistered=run_scope == "acceptance")
        try:
            instrument = REGISTRY.instrument(provider, symbol)
        except ValueError:
            instrument = {
                "provider": provider,
                "symbol": symbol,
                "asset_class": derived[0].asset_class,
                "currency": derived[0].currency,
            }
        input_hash = _hash([row.source_hash for row in source])
        output_hash = _hash([row.model_dump(mode="json") for row in derived])
        paths = write_market_bars(root, derived, part_id=resolved_run_id)
        source_lineage = compact_source_hashes(row.source_hash for row in source)
        lineage = {
            "recipe_id": recipe.recipe_id, "recipe_version": recipe.version,
            "input_dataset": recipe.input_dataset, "input_snapshot_id": input_snapshot.snapshot_id,
            **source_lineage,
            "source_timeframe": recipe.source_timeframe, "target_timeframe": recipe.target_timeframe,
            "session_profile": recipe.session_profile, "calendar_profile": recipe.calendar_profile,
            "materialization": recipe.materialization, "publication_policy": recipe.publication_policy,
            "aggregation_version": self.version, "input_hash": input_hash, "output_hash": output_hash,
            "input_coverage": input_coverage.as_dict(), "output_coverage": output_coverage.as_dict(),
        }
        digests = {
            "recipe_digest": config_digest(recipe),
            "capability_digest": config_digest(capability),
            "instrument_digest": config_digest(instrument),
            "session_profile_digest": config_digest(session),
            "calendar_digest": _hash(recipe.calendar_profile),
            "quality_profile_digest": config_digest(REGISTRY.quality_profile(output.quality_profile)),
        }
        quality = {"status": "pass", "finding_count": 0, "findings": [],
                   "input_coverage": input_coverage.as_dict(),
                   "output_coverage": output_coverage.as_dict()}
        manifest = build_manifest(root, run_id=resolved_run_id, dataset_id=recipe.output_dataset,
                                  schema_version=output.schema_version, paths=paths, row_count=len(derived),
                                  quality_summary=quality, lineage=lineage, run_kind="derive",
                                  run_scope=run_scope, config_digests=digests)
        write_manifest(root, manifest)
        return {
            "run_id": resolved_run_id, "run_kind": "derive", "run_scope": run_scope, "status": "pass",
            "dataset_id": recipe.output_dataset, "schema_version": output.schema_version,
            "provider": derived[0].provider, "symbol": derived[0].symbol,
            "row_count": len(derived), "input_row_count": len(source),
            "min_ts": derived[0].bar_ts.isoformat(), "max_ts": derived[-1].bar_ts.isoformat(),
            "input_hash": input_hash,
            "output_hash": output_hash,
            "paths": [str(path) for path in paths], "manifest": str(manifest_path(root, resolved_run_id)),
            "quality_summary": quality, "lineage": lineage, "config_digests": digests,
            "coverage": {"input": input_coverage.as_dict(), "output": output_coverage.as_dict()},
            "created_at": generated_at.isoformat(),
        }


def recomputation_plan(*, recipe: TransformRecipe, selectors: list[dict[str, str]],
                       affected_start: datetime, affected_end: datetime) -> dict:
    start = _bucket_for_timeframe(affected_start, recipe.target_timeframe)
    end = _next_bucket(
        _bucket_for_timeframe(affected_end + timedelta(microseconds=1), recipe.target_timeframe),
        recipe.target_timeframe,
    )
    return {"input_dataset": recipe.input_dataset, "output_dataset": recipe.output_dataset,
            "recipe_id": recipe.recipe_id, "recipe_version": recipe.version,
            "selectors": selectors, "affected_start": start.isoformat(), "affected_end": end.isoformat(),
            "automatic_execution": False}
