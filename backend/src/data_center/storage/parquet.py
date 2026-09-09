from collections.abc import Iterable
from pathlib import Path
from uuid import uuid4

from data_center.catalog.paths import provider_bars_path
from data_center.domain.models import ProviderBar


def write_provider_bars(root: Path, rows: Iterable[ProviderBar], *, part_id: str | None = None) -> list[Path]:
    records = list(rows)
    if not records:
        raise ValueError("cannot write empty provider_bars dataset")
    try:
        import polars as pl
    except ImportError as exc:
        raise RuntimeError("polars is required for parquet storage") from exc
    paths: list[Path] = []
    shared_part_id = part_id or uuid4().hex
    for year in sorted({record.bar_ts.year for record in records}):
        year_records = [record.model_dump() for record in records if record.bar_ts.year == year]
        first = year_records[0]
        target = provider_bars_path(root, provider=first["provider"], asset_class=first["asset_class"],
                                    symbol=first["symbol"], timeframe=first["timeframe"], year=year)
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"part-{shared_part_id}.parquet"
        if path.exists():
            raise FileExistsError(f"refusing to overwrite immutable part: {path}")
        pl.DataFrame(year_records).write_parquet(path)
        paths.append(path)
    return paths
