from pathlib import Path
from typing import Iterable

from data_center.domain.models import ProviderBar


def write_provider_bars(root: Path, rows: Iterable[ProviderBar]) -> Path:
    records = list(rows)
    if not records:
        raise ValueError("cannot write empty provider_bars dataset")
    target = root / "provider_bars" / f"provider={records[0].provider}" / f"asset_class={records[0].asset_class}" / f"symbol={records[0].symbol}" / f"timeframe={records[0].timeframe}" / f"year={records[0].bar_ts.year}"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "part-000.parquet"
    try:
        import polars as pl
    except ImportError as exc:
        raise RuntimeError("polars is required for parquet storage") from exc
    pl.DataFrame([row.model_dump() for row in records]).write_parquet(path)
    return path

