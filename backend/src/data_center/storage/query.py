from datetime import datetime
from pathlib import Path

import duckdb

from data_center.catalog.manifest import published_files


def _read_current(files: list[Path], partition_columns: list[str], order_columns: str,
                  where: str = "", params: list | None = None, *, bars: bool = False) -> list[dict]:
    if not files:
        return []
    projection = "* replace (cast(bar_ts as timestamptz) as bar_ts, cast(ingest_ts as timestamptz) as ingest_ts)" if bars else "*"
    query = f"""
        with source as (select {projection} from read_parquet(?, union_by_name=true, hive_partitioning=false)), ranked as (
            select *, row_number() over (partition by {', '.join(partition_columns)} order by {order_columns}) as _rn
            from source {where}
        ) select * exclude (_rn) from ranked where _rn = 1 order by {partition_columns[-1]}
    """
    with duckdb.connect() as connection:
        connection.execute("set timezone='UTC'")
        result = connection.execute(query, [[str(path) for path in files], *(params or [])])
        columns = [column[0] for column in result.description]
        return [dict(zip(columns, row)) for row in result.fetchall()]


def query_provider_bars(root: Path, *, symbol: str, timeframe: str, provider: str, start: datetime | None = None, end: datetime | None = None) -> list[dict]:
    files = [path for path in published_files(root, "provider_bars")
             if f"provider={provider}" in path.parts and f"symbol={symbol}" in path.parts and f"timeframe={timeframe}" in path.parts]
    clauses, params = [], []
    if start is not None:
        clauses.append("bar_ts >= ?")
        params.append(start)
    if end is not None:
        clauses.append("bar_ts <= ?")
        params.append(end)
    where = "where " + " and ".join(clauses) if clauses else ""
    return _read_current(files, ["provider", "symbol", "asset_class", "timeframe", "bar_ts"], "ingest_ts desc", where, params, bars=True)


def query_economic_observations(root: Path, *, provider: str, series_id: str, start: str | None = None,
                                end: str | None = None, asof_ts: str | None = None,
                                mode: str = "current") -> list[dict]:
    if mode not in {"current", "pit"}:
        raise ValueError("economic query mode must be current or pit")
    # Preserve the v1 client contract where supplying asof_ts selected PIT mode.
    if mode == "current" and asof_ts is not None:
        mode = "pit"
    if mode == "pit" and not asof_ts:
        raise ValueError("pit query requires asof_ts")
    files = [path for path in published_files(root, "economic_observations")
             if f"provider={provider}" in path.parts and f"series_id={series_id}" in path.parts]
    if not files:
        return []
    clauses, params = [], []
    if start is not None:
        clauses.append("observation_date >= ?")
        params.append(start)
    if end is not None:
        clauses.append("observation_date <= ?")
        params.append(end)
    if mode == "pit":
        # PIT reads exclude unknown release timing instead of treating ingest time as release time.
        # A declared lag is applied to the provider release/vintage boundary.
        clauses.append("cast(asof_ts as timestamptz) <= cast(? as timestamptz) and ((availability_policy = 'realtime_vintage' and cast(vintage_start as date) + coalesce(availability_lag_days, 0) * interval '1 day' <= cast(? as date)) or (availability_policy = 'release_date_known' and cast(release_ts as timestamptz) + coalesce(availability_lag_days, 0) * interval '1 day' <= cast(? as timestamptz)))")
        params.extend([asof_ts, asof_ts[:10], asof_ts])
        order = "case when availability_policy = 'realtime_vintage' then vintage_start else release_ts end desc, asof_ts desc"
    else:
        order = "case when vintage_start is null then 0 else 1 end desc, vintage_start desc nulls last, asof_ts desc"
    where = "where " + " and ".join(clauses) if clauses else ""
    return _read_current(files, ["observation_date"], order, where, params)


def provider_bars_coverage(root: Path, *, provider: str, symbol: str, timeframe: str) -> dict:
    rows = query_provider_bars(root, provider=provider, symbol=symbol, timeframe=timeframe)
    return {
        "dataset_id": "provider_bars",
        "provider": provider,
        "symbol": symbol,
        "timeframe": timeframe,
        "row_count": len(rows),
        "min_ts": rows[0]["bar_ts"] if rows else None,
        "max_ts": rows[-1]["bar_ts"] if rows else None,
    }


def economic_observations_coverage(root: Path, *, provider: str, series_id: str) -> dict:
    rows = query_economic_observations(root, provider=provider, series_id=series_id)
    return {
        "dataset_id": "economic_observations",
        "provider": provider,
        "series_id": series_id,
        "row_count": len(rows),
        "min_date": rows[0]["observation_date"] if rows else None,
        "max_date": rows[-1]["observation_date"] if rows else None,
    }
