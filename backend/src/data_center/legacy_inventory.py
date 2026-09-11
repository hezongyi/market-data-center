"""Read-only inventory for legacy Dukascopy provider-bar Parquet files."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import duckdb

from data_center.capacity import CapacityPolicy
from data_center.evidence import operation_receipt, utc_now, write_receipt
from data_center.settings import Settings


def _source_snapshot(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        stat = path.stat()
        digest.update(str(path).encode())
        digest.update(f"\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def _partition(path: Path) -> tuple[str, str, str, int]:
    values = {}
    for part in path.parts:
        if "=" in part:
            key, value = part.split("=", 1)
            values[key] = value
    return values["asset_class"], values["symbol"], values["timeframe"], int(values["year"])


def _manifest_visible_parts(canonical_root: Path) -> set[Path]:
    visible = set()
    for manifest in (Path(canonical_root) / ".manifests").glob("*.json"):
        try:
            payload = json.loads(manifest.read_text())
            for part in payload.get("parts", []):
                if isinstance(part.get("path"), str):
                    visible.add((Path(canonical_root) / part["path"]).resolve())
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return visible


def inventory_legacy_dukascopy(source_root: Path, evidence_root: Path,
                               capacity_policy: CapacityPolicy | None = None,
                               canonical_root: Path | None = None) -> dict:
    started = utc_now()
    source_root = Path(source_root).resolve()
    all_paths = sorted(source_root.rglob("*.parquet"))
    visible = _manifest_visible_parts(canonical_root) if canonical_root else set()
    paths = [path for path in all_paths if path.resolve() not in visible]
    excluded = [path for path in all_paths if path.resolve() in visible]
    if not paths:
        raise ValueError("no legacy Dukascopy Parquet files found")
    before = _source_snapshot(paths)
    files = defaultdict(lambda: {"file_count": 0, "bytes": 0})
    for path in paths:
        key = _partition(path)
        files[key]["file_count"] += 1
        files[key]["bytes"] += path.stat().st_size

    connection = duckdb.connect(":memory:")
    rows = connection.execute(
        """
        with source as (
          select asset_class, symbol, timeframe, cast(year as integer) as year,
                 try_cast(bar_ts as timestamptz) as ts,
                 coalesce(cast(price_type as varchar), 'unknown') as price_type
          from read_parquet(?, union_by_name=true, hive_partitioning=true)
        ), distinct_times as (
          select distinct asset_class, symbol, timeframe, year, ts from source where ts is not null
        ), ordered as (
          select *, lag(ts) over (
            partition by asset_class, symbol, timeframe, year order by ts
          ) as previous_ts from distinct_times
        ), gaps as (
          select asset_class, symbol, timeframe, year,
                 count(*) filter (where previous_ts is not null and
                   epoch(ts - previous_ts) > case timeframe
                     when '1m' then 60 when '5m' then 300 when '15m' then 900
                     when '30m' then 1800 when '1h' then 3600 when '4h' then 14400
                     when '1d' then 86400 else 0 end) as coverage_gap_count,
                 max(epoch(ts - previous_ts)) as max_gap_seconds
          from ordered group by all
        )
        select s.asset_class, s.symbol, s.timeframe, s.year, count(*) as row_count,
               count(*) - count(distinct s.ts) as duplicate_timestamp_count,
               count(*) filter (where s.ts is null) as invalid_timestamp_count,
               min(s.ts) as min_ts, max(s.ts) as max_ts,
               list(distinct s.price_type order by s.price_type) as price_types,
               g.coverage_gap_count, g.max_gap_seconds
        from source s join gaps g using (asset_class, symbol, timeframe, year)
        group by all order by s.asset_class, s.symbol, s.timeframe, s.year
        """,
        [[str(path) for path in paths]],
    ).fetchall()
    connection.close()
    groups = []
    for row in rows:
        key = (row[0], row[1], row[2], row[3])
        groups.append({
            "asset_class": row[0], "symbol": row[1], "timeframe": row[2], "year": row[3],
            **files[key], "row_count": row[4], "duplicate_timestamp_count": row[5],
            "invalid_timestamp_count": row[6],
            "min_ts": row[7].isoformat() if row[7] else None,
            "max_ts": row[8].isoformat() if row[8] else None,
            "price_types": row[9], "coverage_gap_count": row[10], "max_gap_seconds": row[11],
        })
    after = _source_snapshot(paths)
    capacity = (capacity_policy or CapacityPolicy()).inspect(source_root).as_dict()
    details = {
        "source_kind": "legacy_dukascopy_provider_bars",
        "inventory_mode": "read_only_no_manifest_publication",
        "source_snapshot_before": before,
        "source_snapshot_after": after,
        "source_mutated": before != after,
        "manifest_visible_files_excluded": len(excluded),
        "manifest_visible_bytes_excluded": sum(path.stat().st_size for path in excluded),
        "file_count": len(paths),
        "row_count": sum(group["row_count"] for group in groups),
        "bytes": sum(path.stat().st_size for path in paths),
        "estimated_publication_bytes": sum(path.stat().st_size for path in paths),
        "capacity": capacity,
        "bulk_migration_allowed": capacity["status"] == "ok",
        "groups": groups,
    }
    result = "pass" if before == after else "failed"
    receipt = operation_receipt(
        action="dukascopy_legacy_inventory",
        command="python -m data_center.legacy_inventory",
        started_at=started,
        result=result,
        failure_stage=None if result == "pass" else "source_immutability",
        details=details,
    )
    path = write_receipt(evidence_root, receipt)
    return {**receipt, "receipt": str(path) if path else None}


def main() -> None:
    settings = Settings()
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--canonical-root", type=Path, default=settings.canonical_root)
    parser.add_argument("--evidence-root", type=Path, default=settings.evidence_root)
    args = parser.parse_args()
    report = inventory_legacy_dukascopy(args.source_root, args.evidence_root, settings.capacity_policy(),
                                        args.canonical_root)
    print(json.dumps({key: value for key, value in report.items() if key != "details"}), flush=True)
    raise SystemExit(0 if report["result"] == "pass" else 1)


if __name__ == "__main__":
    main()
