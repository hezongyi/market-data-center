from pathlib import Path
from uuid import uuid4


def write_economic_observations(root: Path, rows: list[dict], *, part_id: str | None = None) -> Path:
    if not rows:
        raise ValueError("cannot write empty economic observations")
    import polars as pl
    series = rows[0]["series_id"]
    provider = rows[0]["provider"]
    target = root / "economic_observations" / f"provider={provider}" / f"series_id={series}"
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"part-{part_id or uuid4().hex}.parquet"
    pl.DataFrame(rows).write_parquet(path)
    return path
