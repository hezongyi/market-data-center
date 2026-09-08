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

