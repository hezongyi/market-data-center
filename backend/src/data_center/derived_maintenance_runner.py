"""Generic, snapshot-aware maintenance for derived market bars.

The raw maintenance runner owns provider windows.  This module owns the next
stage of the graph: it resolves registered recipes, builds the smallest
affected output window, skips a materialization that already points at the
same immutable input snapshot, and submits each remaining derive job through
the read/write API.  No provider-specific aggregation code belongs here.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from data_center.catalog.snapshot import Catalog
from data_center.domain.errors import PROVIDER_GAP_ERROR_TYPES
from data_center.evidence import operation_receipt, write_receipt
from data_center.maintenance_runner import approved_targets
from data_center.platform_registry import REGISTRY
from data_center.settings import Settings
from data_center.transform import recomputation_plan


@dataclass(frozen=True)
class DerivedTarget:
    provider: str
    symbol: str
    asset_class: str


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("derived maintenance timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _recipe_key(recipe) -> tuple[int, int, str, str]:
    """Sort source layers before their downstream calendar rollups."""
    source_rank = 0 if recipe.input_dataset == "provider_bars" else 1
    # The registry stores the canonical timeframe strings; keeping the sort
    # deterministic makes dry-run plans and receipts reproducible.
    return source_rank, len(recipe.source_timeframe), recipe.source_timeframe, recipe.recipe_id


def select_recipes(recipe_specs: list[str] | None = None) -> tuple:
    """Resolve ``recipe_id@version`` specs, or all canonical persisted recipes."""
    recipes = REGISTRY.recipes()
    if recipe_specs:
        selected = []
        for spec in recipe_specs:
            if "@" in spec:
                recipe_id, version = spec.rsplit("@", 1)
            else:
                recipe_id, version = spec, None
            matches = [item for item in recipes if item.recipe_id == recipe_id
                       and (version is None or item.version == version)]
            if len(matches) != 1:
                raise ValueError(f"recipe is not uniquely registered: {spec}")
            selected.append(matches[0])
        recipes = tuple(selected)
    return tuple(sorted(
        (item for item in recipes
         if item.materialization == "persisted" and item.publication_policy == "canonical"),
        key=_recipe_key,
    ))


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return _utc(datetime.fromisoformat(value))


def _manifest_covers(*, root: Path, provider: str, symbol: str, recipe, input_snapshot_id: str,
                     start: datetime, end: datetime) -> bool:
    """Return true only for a ready output tied to this exact input snapshot."""
    manifests = sorted((Path(root) / ".manifests").glob("*.json"))
    for path in manifests:
        try:
            manifest = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("dataset_id") != recipe.output_dataset or manifest.get("status") != "published":
            continue
        lineage = manifest.get("lineage") or {}
        if any(lineage.get(field) != value for field, value in (
            ("recipe_id", recipe.recipe_id),
            ("recipe_version", recipe.version),
            ("input_snapshot_id", input_snapshot_id),
            ("target_timeframe", recipe.target_timeframe),
        )):
            continue
        coverage = lineage.get("output_coverage") or {}
        if coverage.get("readiness_status") != "ready":
            continue
        minimum, maximum = _parse_iso(coverage.get("min_ts")), _parse_iso(coverage.get("max_ts"))
        if minimum is None or maximum is None or minimum > start:
            continue
        # A materialized bar covers its own target interval.  The coverage
        # metadata contains the cadence for fixed/week units; month is handled
        # conservatively by requiring the requested end not to exceed max_ts.
        if recipe.target_timeframe == "1mo":
            covered_end = maximum
        else:
            seconds = int(coverage.get("timeframe_seconds") or 0)
            covered_end = maximum if seconds <= 0 else maximum.timestamp() + seconds
            covered_end = datetime.fromtimestamp(covered_end, tz=timezone.utc)
        if covered_end < end:
            continue
        part_text = " ".join(str(item.get("path", "")) for item in manifest.get("parts", ()))
        required = (f"provider={provider}", f"symbol={symbol}", f"timeframe={recipe.target_timeframe}")
        if all(token in part_text for token in required):
            return True
    return False


def plan_derived_maintenance(*, root: Path, provider: str, targets: tuple[DerivedTarget, ...],
                             recipes: tuple, start: datetime, end: datetime) -> list[dict]:
    """Build a deterministic dependency-aware plan without writing anything."""
    start, end = _utc(start), _utc(end)
    if end <= start:
        raise ValueError("derived maintenance end must be after start")
    catalog = Catalog(root)
    planned: list[dict] = []
    for target in targets:
        for recipe in recipes:
            selector = {"provider": target.provider, "symbol": target.symbol,
                        "timeframe": recipe.source_timeframe}
            snapshot = catalog.resolve(recipe.input_dataset, selector)
            item = {
                "provider": target.provider, "symbol": target.symbol,
                "asset_class": target.asset_class,
                "recipe_id": recipe.recipe_id, "recipe_version": recipe.version,
                "input_dataset": recipe.input_dataset, "output_dataset": recipe.output_dataset,
                "source_timeframe": recipe.source_timeframe,
                "target_timeframe": recipe.target_timeframe,
                "input_snapshot_id": snapshot.snapshot_id,
                "input_part_count": len(snapshot.parts),
            }
            if not snapshot.parts:
                item.update(status="not_ready", reason="input_snapshot_empty")
                planned.append(item)
                continue
            recompute = recomputation_plan(
                recipe=recipe, selectors=[{"provider": target.provider, "symbol": target.symbol}],
                # The maintenance request is half-open.  ``recomputation_plan``
                # accepts an affected timestamp (inclusive) and adds one
                # microsecond internally, so stay two microseconds inside the
                # interval to avoid an extra bucket when ``end`` is aligned.
                affected_start=start, affected_end=end - timedelta(microseconds=2),
            )
            window_start, window_end = _utc(datetime.fromisoformat(recompute["affected_start"])), _utc(
                datetime.fromisoformat(recompute["affected_end"])
            )
            item["window"] = {"start": window_start.isoformat(), "end": window_end.isoformat(),
                              "semantics": "half-open"}
            if _manifest_covers(root=root, provider=target.provider, symbol=target.symbol,
                                recipe=recipe, input_snapshot_id=snapshot.snapshot_id,
                                start=window_start, end=window_end):
                item.update(status="already_materialized", reason="same_input_snapshot")
            else:
                item.update(status="planned", reason="input_snapshot_changed_or_missing")
            planned.append(item)
    return planned


def _request(session: requests.Session, base_url: str, method: str, path: str, **kwargs) -> dict:
    response = session.request(method, base_url.rstrip("/") + "/api/v1" + path, timeout=30, **kwargs)
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(f"Data Center API returned errors for {path}")
    return payload["data"]


def _wait_run(session: requests.Session, base_url: str, run_id: str, deadline: float) -> dict:
    while True:
        receipt = _request(session, base_url, "GET", f"/runs/{run_id}")
        if receipt.get("status") in {"pass", "failed", "dead_letter"}:
            return receipt
        if time.monotonic() >= deadline:
            raise TimeoutError(f"derived run polling deadline exceeded: {run_id}")
        time.sleep(1.0)


def run_derived_maintenance(*, base_url: str, root: Path, evidence_root: Path,
                            provider: str, symbols: list[str] | None,
                            start: datetime, end: datetime,
                            recipe_specs: list[str] | None = None,
                            run_scope: str = "maintenance", dry_run: bool = False,
                            poll_deadline_seconds: float = 900.0) -> dict:
    """Plan and, unless dry-run, execute derived jobs through the API/worker."""
    started = datetime.now(timezone.utc).isoformat()
    recipes = select_recipes(recipe_specs)
    approved = approved_targets(provider, symbols)
    targets = tuple(DerivedTarget(item.provider, item.symbol, item.asset_class) for item in approved)
    if not targets:
        raise ValueError(f"no approved instruments selected for {provider}")
    if not recipes:
        raise ValueError("no canonical persisted recipes are registered")
    session = requests.Session()
    session.trust_env = False
    api_key = os.getenv("DATACENTER_API_KEY")
    if api_key:
        session.headers["X-API-Key"] = api_key
    # Execute one recipe layer at a time.  A downstream recipe (1d -> 1w or
    # 1mo) must resolve a fresh catalog snapshot after its upstream 1m -> 1d
    # run is published; planning the whole graph against one stale snapshot
    # would incorrectly mark that layer as empty.
    plan: list[dict] = []
    failures = 0
    try:
        for recipe in recipes:
            layer = plan_derived_maintenance(
                root=root, provider=provider, targets=targets, recipes=(recipe,), start=start, end=end,
            )
            plan.extend(layer)
            for item in layer:
                if item["status"] != "planned" or dry_run:
                    continue
                try:
                    window = item["window"]
                    submitted = _request(session, base_url, "POST", "/derive/runs", json={
                        "job_id": f"derived-maintenance-{provider.lower()}-{item['symbol'].lower()}-"
                                  f"{item['target_timeframe']}-{item['input_snapshot_id'][:12]}",
                        "provider": provider, "symbol": item["symbol"],
                        "recipe_id": item["recipe_id"], "recipe_version": item["recipe_version"],
                        "start": window["start"], "end": window["end"],
                        "input_snapshot_id": item["input_snapshot_id"], "run_scope": run_scope,
                    })
                    run_id = submitted["run_id"]
                    receipt = _wait_run(session, base_url, run_id, time.monotonic() + poll_deadline_seconds)
                    item["run_id"] = run_id
                    item["run_status"] = receipt.get("status")
                    item["row_count"] = receipt.get("row_count")
                    item["output_hash"] = receipt.get("output_hash")
                    if receipt.get("status") != "pass":
                        # Provider-verified gaps and empty session windows are
                        # expected for sparse historical data.  Keep the
                        # item visible as degraded (never synthesize bars),
                        # while allowing independent symbols to complete.
                        if receipt.get("error_type") in PROVIDER_GAP_ERROR_TYPES:
                            item["status"] = "degraded"
                            item["reason"] = "provider_gap_or_empty_session"
                        else:
                            failures += 1
                            item["status"] = "failed"
                            item["reason"] = receipt.get("error_type") or "derive_run_failed"
                except Exception as exc:  # noqa: BLE001 - continue independent symbols/recipes
                    failures += 1
                    item.update(status="failed", reason=type(exc).__name__)
    finally:
        session.close()
    details = {
        "provider": provider, "run_scope": run_scope, "dry_run": dry_run,
        "requested_start": _utc(start).isoformat(), "requested_end": _utc(end).isoformat(),
        "target_count": len(targets), "recipe_count": len(recipes),
        "planned_count": sum(item["status"] == "planned" for item in plan),
        "already_materialized_count": sum(item["status"] == "already_materialized" for item in plan),
        "not_ready_count": sum(item["status"] == "not_ready" for item in plan),
        "failed_count": failures,
        "degraded_count": sum(item["status"] == "degraded" for item in plan),
        "dependency_graph": REGISTRY.dependency_graph(), "plan": plan,
    }
    # A dry-run is a planning operation: an empty downstream snapshot is an
    # expected finding before its upstream layer has been materialized, not an
    # execution failure.  Real runs still fail closed on any not-ready input.
    result = "pass" if dry_run or (failures == 0 and details["not_ready_count"] == 0) else "failed"
    receipt = operation_receipt(
        action="derived_market_bars_maintenance",
        command="python -m data_center.derived_maintenance_runner",
        started_at=started, result=result,
        failure_stage="derive" if result == "failed" else None,
        error_category="DerivedMaintenanceError" if result == "failed" else None,
        details=details,
    )
    path = write_receipt(evidence_root, receipt)
    return {**receipt, "receipt": str(path) if path else None}


def main() -> None:
    settings = Settings()
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18380")
    parser.add_argument("--provider", default="dukascopy")
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--recipes", nargs="*", default=None,
                        help="recipe_id[@version] values; default is all canonical recipes")
    parser.add_argument("--start", type=datetime.fromisoformat, required=True)
    parser.add_argument("--end", type=datetime.fromisoformat, required=True)
    parser.add_argument("--run-scope", choices=("maintenance", "production", "acceptance"), default="maintenance")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    configured_symbols = settings.maintenance_symbol_list()
    selected_symbols = args.symbols if args.symbols is not None else configured_symbols or None
    report = run_derived_maintenance(
        base_url=args.base_url, root=settings.canonical_root, evidence_root=settings.evidence_root,
        provider=args.provider, symbols=selected_symbols, start=args.start, end=args.end,
        recipe_specs=args.recipes, run_scope=args.run_scope, dry_run=args.dry_run,
    )
    print(json.dumps({"result": report["result"], "receipt": report["receipt"],
                      "details": {key: report["details"][key] for key in (
                          "target_count", "recipe_count", "planned_count", "already_materialized_count",
                          "not_ready_count", "failed_count")}}, sort_keys=True))
    raise SystemExit(0 if report["result"] == "pass" else 1)


if __name__ == "__main__":
    main()
