#!/usr/bin/env python3
"""Run the market-data platform contract against real local storage and DuckDB."""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from data_center.catalog.manifest import immutable_json
from data_center.catalog.snapshot import Catalog
from data_center.control_plane import (
    IngestWindow,
    MaintenancePolicy,
    SessionProfile,
    TransformRecipe,
)
from data_center.domain.models import IngestJob, ProviderBar
from data_center.evidence import operation_receipt, write_receipt
from data_center.platform import build_ingest_plan, coverage_for_rows, execute_ingest
from data_center.settings import Settings
from data_center.storage.query import query_market_bars, query_provider_bars
from data_center.transform import TransformExecutor, recomputation_plan


def source_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1], text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


class ControlledConnector:
    version = "platform-acceptance.v1"

    def __init__(self, provider: str) -> None:
        self.provider = provider

    def fetch_bars(self, job: IngestJob) -> list[ProviderBar]:
        rows = []
        for index in range(10):
            stamp = job.start + timedelta(minutes=index)
            rows.append(ProviderBar(
                symbol=job.symbol, asset_class=job.asset_class, provider=job.provider,
                timeframe=job.timeframe, bar_ts=stamp, open=100 + index,
                high=102 + index, low=99 + index, close=101 + index, volume=10 + index,
                currency="USD", price_type="bid" if job.provider == "dukascopy" else "raw",
                ingest_ts=datetime.now(timezone.utc), source_hash=f"{job.provider}-{job.job_id}-{index}",
            ))
        return rows


def run(root: Path, output: Path, evidence_root: Path | None = None) -> dict:
    started = datetime.now(timezone.utc)
    acceptance_id = uuid4().hex
    settings = Settings(canonical_root=root)
    capacity_before = settings.capacity_policy().require_ingest_capacity(root).as_dict()
    base = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    providers = (("fixture", "PLATFORMFIXTURE"), ("binance", "PLATFORMUSDT"),
                 ("dukascopy", "PLATUSD"))
    recipe = TransformRecipe(
        recipe_id="platform-acceptance-5m", version="1", input_dataset="provider_bars",
        output_dataset="market_bars", source_timeframe="1m", target_timeframe="5m",
        allowed_schema_versions=("provider_bars.v1",), missing_input_policy="fail",
    )
    executor = TransformExecutor()
    results = []
    for provider, symbol in providers:
        job = IngestJob(
            job_id=f"platform-{acceptance_id}-{provider}", provider=provider, symbol=symbol,
            asset_class="crypto" if provider != "dukascopy" else "fx", timeframe="1m",
            start=base, end=base + timedelta(minutes=10), run_kind="backfill", run_scope="acceptance",
        )
        plan = build_ingest_plan(job=job, policy=MaintenancePolicy(shard_days=7))
        window_data = plan["windows"][0]
        window = IngestWindow(datetime.fromisoformat(window_data["start"]),
                              datetime.fromisoformat(window_data["end"]),
                              window_data["reason"], window_data["ordinal"])
        raw = execute_ingest(
            window=window, job=job, root=root, connector=ControlledConnector(provider),
            run_id=f"platform-raw-{acceptance_id}-{provider}", execution_plan=plan,
        )
        raw_rows = query_provider_bars(root, provider=provider, symbol=symbol, timeframe="1m",
                                       start=base, end=base + timedelta(minutes=10))
        coverage = coverage_for_rows(
            dataset_id="provider_bars",
            selector={"provider": provider, "symbol": symbol, "timeframe": "1m"}, rows=raw_rows,
            session_profile=SessionProfile(profile_id="acceptance", mode="continuous"),
            requested_start=base, requested_end=base + timedelta(minutes=10),
        )
        if coverage.readiness_status != "ready":
            raise RuntimeError(f"{provider} raw coverage is not ready")
        snapshot = Catalog(root).resolve(
            "provider_bars", {"provider": provider, "symbol": symbol, "timeframe": "1m"},
        )
        derived = executor.derive(
            recipe=recipe, input_snapshot=snapshot, selector={"provider": provider, "symbol": symbol},
            start=base, end=base + timedelta(minutes=10), root=root,
            run_id=f"platform-derived-{acceptance_id}-{provider}", run_scope="acceptance",
        )
        derived_rows = query_market_bars(
            root, provider=provider, symbol=symbol, timeframe="5m",
            price_basis="bid" if provider == "dukascopy" else "raw",
            recipe_id=recipe.recipe_id, recipe_version=recipe.version,
            start=base, end=base + timedelta(minutes=10),
        )
        if len(derived_rows) != 2 or derived_rows[0]["input_snapshot_id"] != snapshot.snapshot_id:
            raise RuntimeError(f"{provider} derived readback failed")
        results.append({"provider": provider, "raw_run_id": raw["run_id"],
                        "raw_rows": len(raw_rows), "coverage": coverage.as_dict(),
                        "snapshot_id": snapshot.snapshot_id, "derived_run_id": derived["run_id"],
                        "derived_rows": len(derived_rows), "lineage": derived["lineage"]})
    recompute = recomputation_plan(
        recipe=recipe, selectors=[{"provider": provider, "symbol": symbol}
                                  for provider, symbol in providers],
        affected_start=base + timedelta(minutes=1), affected_end=base + timedelta(minutes=6),
    )
    details = {
        "acceptance_id": acceptance_id, "run_kind": "parity", "run_scope": "acceptance",
        "canonical_root": str(root.resolve()), "capacity_before": capacity_before,
        "capacity_after": settings.capacity_policy().inspect(root).as_dict(),
        "providers": results, "executor_version": executor.version,
        "recomputation_plan": recompute,
        "source_commit": source_commit(), "working_tree": "dirty" if subprocess.run(
            ["git", "status", "--porcelain"], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, check=False,
        ).stdout.strip() else "clean",
    }
    receipt = operation_receipt(
        action="platform_acceptance", command="scripts/platform_acceptance.py",
        started_at=started.isoformat(), result="pass", details=details,
    )
    receipt["acceptance_id"] = acceptance_id
    receipt["providers"] = results
    receipt["recomputation_plan"] = recompute
    immutable_json(output, receipt)
    if evidence_root is not None:
        write_receipt(evidence_root, receipt)
    return receipt


def main() -> None:
    settings = Settings()
    parser = argparse.ArgumentParser()
    parser.add_argument("--canonical-root", type=Path, default=settings.canonical_root)
    parser.add_argument("--output", type=Path,
                        default=settings.evidence_root / "platform" / f"acceptance-{uuid4().hex}.json")
    parser.add_argument("--evidence-root", type=Path, default=None)
    args = parser.parse_args()
    report = run(args.canonical_root, args.output, args.evidence_root)
    print(json.dumps({"status": report["result"], "acceptance_id": report["acceptance_id"],
                      "providers": [item["provider"] for item in report["providers"]],
                      "output": str(args.output)}, sort_keys=True))


if __name__ == "__main__":
    main()
