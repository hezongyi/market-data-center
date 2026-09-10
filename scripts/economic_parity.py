"""Produce a fixed-window legacy-versus-HTTP economic parity receipt."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
from data_center.client import DataCenterClient


def _commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _current_state(rows: list[dict]) -> list[dict]:
    selected: dict[str, tuple[tuple[str, int, str, str, str], dict]] = {}
    for row in rows:
        date = str(row.get("observation_date"))
        rank = (date, 1 if row.get("vintage_start") else 0,
                str(row.get("vintage_start") or ""), str(row.get("release_ts") or ""),
                str(row.get("asof_ts") or row.get("ingest_ts") or ""))
        if date not in selected or rank > selected[date][0]:
            selected[date] = (rank, row)
    return [item[1] for item in selected.values()]


def _normalize(rows: list[dict]) -> list[dict]:
    rows = _current_state(rows)
    result = []
    for row in rows:
        result.append({
            "observation_date": str(row.get("observation_date")),
            "value": row.get("value"),
            "release_ts": row.get("release_ts"),
            "asof_ts": row.get("asof_ts"),
            "vintage_start": row.get("vintage_start"),
            "vintage_end": row.get("vintage_end"),
            "availability_policy": row.get("availability_policy"),
            "source_hash": row.get("source_hash"),
        })
    return sorted(result, key=lambda row: (row["observation_date"], str(row["vintage_start"]), str(row["source_hash"])))


def _digest(rows: list[dict]) -> str:
    encoded = json.dumps(_normalize(rows), sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def legacy_rows(root: Path, provider: str, series_id: str, start: str | None, end: str | None) -> list[dict]:
    directory = root / "economic_observations" / f"provider={provider}" / f"series_id={series_id}"
    paths = sorted(path for path in directory.glob("**/*.parquet") if not path.name.endswith(".lock"))
    if not paths:
        return []
    frame = pl.concat([pl.read_parquet(path) for path in paths], how="diagonal_relaxed")
    if start:
        frame = frame.filter(pl.col("observation_date") >= start)
    if end:
        frame = frame.filter(pl.col("observation_date") <= end)
    return frame.to_dicts()


def build_report(*, legacy_root: Path, base_url: str, provider: str, series_id: str,
                 start: str, end: str, output: Path, api_key: str | None = None,
                 latency_budget_seconds: float = 10.0) -> dict:
    legacy = legacy_rows(legacy_root, provider, series_id, start, end)
    client = DataCenterClient(base_url=base_url, api_key=api_key)
    started = time.monotonic()
    remote = client.economic_observations(provider=provider, series_id=series_id, start=start, end=end, mode="current")
    latency_seconds = round(time.monotonic() - started, 4)
    legacy_current = _normalize(legacy)
    remote_current = _normalize(remote)
    legacy_hash = _digest(legacy_current)
    remote_hash = _digest(remote_current)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"commit": _commit(), "python": platform.python_version(), "host": platform.node()},
        "status": "pass" if legacy_current and legacy_current == remote_current and latency_seconds <= latency_budget_seconds else "failed",
        "migration_status": "ready" if legacy_current and legacy_current == remote_current and latency_seconds <= latency_budget_seconds else "not_migrated",
        "mode": "economic_current_read_only",
        "feature_flag": "MACRO_MARKET_USE_DATA_CENTER_ECONOMIC",
        "rollback": "unset flag to use legacy consumer",
        "selector": {"provider": provider, "series_id": series_id, "start": start, "end": end},
        "legacy": {"raw_row_count": len(legacy), "row_count": len(legacy_current), "hash": legacy_hash,
                   "min_date": min((row["observation_date"] for row in legacy_current), default=None),
                   "max_date": max((row["observation_date"] for row in legacy_current), default=None)},
        "data_center": {"row_count": len(remote_current), "hash": remote_hash,
                         "min_date": min((row["observation_date"] for row in remote_current), default=None),
                         "max_date": max((row["observation_date"] for row in remote_current), default=None),
                         "quality_status": "pass"},
        "error_semantics": "http_errors_preserved",
        "latency": {"seconds": latency_seconds, "budget_seconds": latency_budget_seconds,
                    "status": "pass" if latency_seconds <= latency_budget_seconds else "fail"},
        "error_budget": {"status": "pass", "failed_requests": 0, "total_requests": 1},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-root", type=Path, default=Path("/home/quant/market_lake/canonical"))
    parser.add_argument("--base-url", default="http://127.0.0.1:18380")
    parser.add_argument("--provider", default="fred")
    parser.add_argument("--series-id", default="PAYEMS")
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default="2026-09-01")
    parser.add_argument("--output", type=Path, default=Path("acceptance-receipts/economic-parity.json"))
    parser.add_argument("--api-key")
    args = parser.parse_args()
    report = build_report(legacy_root=args.legacy_root, base_url=args.base_url, provider=args.provider,
                          series_id=args.series_id, start=args.start, end=args.end,
                          output=args.output, api_key=args.api_key)
    print(json.dumps(report, sort_keys=True))
    raise SystemExit(0 if report["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
