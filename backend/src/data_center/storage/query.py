from datetime import datetime
from pathlib import Path


def query_provider_bars(root: Path, *, symbol: str, timeframe: str, start: datetime | None = None, end: datetime | None = None) -> list[dict]:
    import polars as pl
    pattern = root / "provider_bars" / "**" / f"symbol={symbol}" / f"timeframe={timeframe}" / "**" / "*.parquet"
    files = list(root.glob(str(pattern.relative_to(root))))
    if not files:
        return []
    frame = pl.read_parquet(files)
    if start is not None:
        frame = frame.filter(pl.col("bar_ts") >= start)
    if end is not None:
        frame = frame.filter(pl.col("bar_ts") <= end)
    return frame.sort("bar_ts").to_dicts()


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
