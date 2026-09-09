from pathlib import Path
from uuid import uuid4

from data_center.catalog.paths import economic_observations_path


def write_economic_observations(root: Path, rows: list[dict], *, part_id: str | None = None) -> Path:
    if not rows:
        raise ValueError("cannot write empty economic observations")
    import polars as pl
    series = rows[0]["series_id"]
    provider = rows[0]["provider"]
    target = economic_observations_path(root, provider=provider, series_id=series)
    target.mkdir(parents=True, exist_ok=True)
    resolved_part_id = part_id or uuid4().hex
    path = target / f"part-{resolved_part_id}.parquet"
    if path.exists():
        raise FileExistsError(f"refusing to overwrite immutable part: {path}")
    pl.DataFrame(rows).write_parquet(path)
    return path
