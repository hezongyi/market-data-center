from datetime import datetime
from pathlib import Path


def query_provider_bars(root: Path, *, symbol: str, timeframe: str, provider: str, start: datetime | None = None, end: datetime | None = None) -> list[dict]:
    import polars as pl
    pattern = root / "provider_bars" / f"provider={provider}" / "**" / f"symbol={symbol}" / f"timeframe={timeframe}" / "**" / "*.parquet"
    files = list(root.glob(str(pattern.relative_to(root))))
    if not files:
        return []
    frame = pl.read_parquet(files)
    if start is not None:
        frame = frame.filter(pl.col("bar_ts") >= start)
    if end is not None:
        frame = frame.filter(pl.col("bar_ts") <= end)
    current = (
        frame.sort(["bar_ts", "ingest_ts"])
        .group_by(["provider", "symbol", "asset_class", "timeframe", "bar_ts"], maintain_order=True)
        .last()
        .sort("bar_ts")
    )
    return current.to_dicts()


def query_economic_observations(root: Path, *, provider: str, series_id: str, start: str | None = None, end: str | None = None) -> list[dict]:
    import polars as pl

    base = root / "economic_observations" / f"provider={provider}" / f"series_id={series_id}"
    files = sorted(base.glob("*.parquet"))
    if not files:
        return []
    frame = pl.read_parquet(files)
    if start is not None:
        frame = frame.filter(pl.col("observation_date") >= start)
    if end is not None:
        frame = frame.filter(pl.col("observation_date") <= end)
    latest = (
        frame.sort(["observation_date", "vintage_start", "asof_ts"], nulls_last=True)
        .group_by("observation_date", maintain_order=True)
        .last()
        .sort("observation_date")
    )
    return latest.to_dicts()


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
