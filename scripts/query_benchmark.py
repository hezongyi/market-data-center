"""Generate a governed 1M-row/1K-part fixture and benchmark bounded queries."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import resource
import statistics
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
from data_center.catalog.manifest import build_manifest, write_manifest
from data_center.catalog.paths import provider_bars_path
from data_center.storage.query import QueryEngine


def commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def generate(root: Path, *, parts: int, rows_per_part: int) -> None:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for part_number in range(parts):
        offset = part_number * rows_per_part
        timestamps = [start + timedelta(minutes=offset + row) for row in range(rows_per_part)]
        values = [float(offset + row + 1) for row in range(rows_per_part)]
        year = timestamps[0].year
        target = provider_bars_path(root, provider="benchmark", asset_class="synthetic",
                                    symbol="SCALE", timeframe="1m", year=year)
        target.mkdir(parents=True, exist_ok=True)
        frame = pl.DataFrame({
            "symbol": ["SCALE"] * rows_per_part,
            "asset_class": ["synthetic"] * rows_per_part,
            "provider": ["benchmark"] * rows_per_part,
            "timeframe": ["1m"] * rows_per_part,
            "bar_ts": timestamps,
            "open": values,
            "high": [value + 1 for value in values],
            "low": [value - 1 for value in values],
            "close": [value + 0.5 for value in values],
            "volume": values,
            "currency": ["USD"] * rows_per_part,
            "price_type": ["raw"] * rows_per_part,
            "ingest_ts": [timestamp + timedelta(days=1) for timestamp in timestamps],
            "source_hash": [f"benchmark-{offset + row}" for row in range(rows_per_part)],
        })
        path = target / f"part-{part_number:04d}.parquet"
        frame.write_parquet(path)
        manifest = build_manifest(
            root, run_id=f"benchmark-{part_number:04d}", dataset_id="provider_bars",
            schema_version="provider_bars.v1", paths=[path], row_count=rows_per_part,
            quality_summary={"status": "pass", "finding_count": 0, "findings": []},
        )
        write_manifest(root, manifest)


def percentile95(values: list[float]) -> float:
    return statistics.quantiles(values, n=100, method="inclusive")[94] if len(values) > 1 else values[0]


def run_benchmark(root: Path, *, parts: int, rows_per_part: int, samples: int) -> dict:
    generate_started = time.monotonic()
    generate(root, parts=parts, rows_per_part=rows_per_part)
    generated_seconds = time.monotonic() - generate_started
    baseline_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    engine = QueryEngine(root)
    cold_started = time.monotonic()
    cold = engine.provider_bars_page(provider="benchmark", symbol="SCALE", timeframe="1m", page_size=1_000)
    cold_seconds = time.monotonic() - cold_started
    warm = []
    for _ in range(samples):
        started = time.monotonic()
        page = engine.provider_bars_page(provider="benchmark", symbol="SCALE", timeframe="1m", page_size=1_000)
        warm.append(time.monotonic() - started)
        assert page.rows == cold.rows and page.snapshot_id == cold.snapshot_id
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mib = max(0, peak_rss - baseline_rss) / 1024
    digest = hashlib.sha256(json.dumps(cold.rows, sort_keys=True, default=str).encode()).hexdigest()
    warm_p95 = percentile95(warm)
    checks = {
        "part_count": parts >= 1_000,
        "row_count": parts * rows_per_part >= 1_000_000,
        "page_count": cold.count == 1_000,
        "warm_p95": warm_p95 < 1.0,
        "cold": cold_seconds < 3.0,
        "rss": rss_mib < 512.0,
    }
    return {
        "status": "pass" if all(checks.values()) else "failed",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"commit": commit(), "host": platform.node(), "python": platform.python_version()},
        "dataset": {"parts": parts, "rows": parts * rows_per_part, "rows_per_part": rows_per_part,
                    "generation_seconds": round(generated_seconds, 4)},
        "query": {"page_size": 1_000, "cold_seconds": round(cold_seconds, 4),
                  "warm_samples": samples, "warm_p95_seconds": round(warm_p95, 4),
                  "additional_peak_rss_mib": round(rss_mib, 2), "output_hash": digest,
                  "snapshot_id": cold.snapshot_id},
        "checks": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parts", type=int, default=1_000)
    parser.add_argument("--rows-per-part", type=int, default=1_000)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--output", type=Path, default=Path("acceptance-receipts/query-benchmark.json"))
    args = parser.parse_args()
    if args.root:
        args.root.mkdir(parents=True, exist_ok=False)
        report = run_benchmark(args.root, parts=args.parts, rows_per_part=args.rows_per_part,
                               samples=args.samples)
    else:
        with tempfile.TemporaryDirectory(prefix="mdc-query-benchmark-") as directory:
            report = run_benchmark(Path(directory), parts=args.parts, rows_per_part=args.rows_per_part,
                                   samples=args.samples)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(report, sort_keys=True))
    raise SystemExit(0 if report["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
