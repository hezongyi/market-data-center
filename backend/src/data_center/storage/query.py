from datetime import datetime
from pathlib import Path

import duckdb


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
    pattern = root / "provider_bars" / f"provider={provider}" / "**" / f"symbol={symbol}" / f"timeframe={timeframe}" / "**" / "*.parquet"
    files = list(root.glob(str(pattern.relative_to(root))))
    clauses, params = [], []
    if start is not None:
        clauses.append("bar_ts >= ?")
        params.append(start)
    if end is not None:
        clauses.append("bar_ts <= ?")
        params.append(end)
    where = "where " + " and ".join(clauses) if clauses else ""
    return _read_current(files, ["provider", "symbol", "asset_class", "timeframe", "bar_ts"], "ingest_ts desc", where, params, bars=True)


def query_economic_observations(root: Path, *, provider: str, series_id: str, start: str | None = None, end: str | None = None) -> list[dict]:
    base = root / "economic_observations" / f"provider={provider}" / f"series_id={series_id}"
    files = sorted(base.glob("*.parquet"))
    if not files:
        return []
    clauses, params = [], []
    if start is not None:
        clauses.append("observation_date >= ?")
        params.append(start)
    if end is not None:
        clauses.append("observation_date <= ?")
        params.append(end)
    where = "where " + " and ".join(clauses) if clauses else ""
    return _read_current(files, ["observation_date"], "case when vintage_start is null then 0 else 1 end desc, vintage_start desc nulls last, asof_ts desc", where, params)


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
