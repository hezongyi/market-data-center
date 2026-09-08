from pathlib import Path


def write_economic_observations(root: Path, rows: list[dict]) -> Path:
    if not rows:
        raise ValueError("cannot write empty economic observations")
    import polars as pl
    series = rows[0]["series_id"]
    provider = rows[0]["provider"]
    target = root / "economic_observations" / f"provider={provider}" / f"series_id={series}"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "part-000.parquet"
    pl.DataFrame(rows).write_parquet(path)
    return path

