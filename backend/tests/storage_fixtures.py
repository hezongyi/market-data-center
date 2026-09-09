"""Explicit publication of small validated fixtures for query contract tests."""
from data_center.catalog.manifest import build_manifest, write_manifest
from data_center.storage.economic import (
    write_economic_observations as write_economic_part,
)
from data_center.storage.parquet import write_provider_bars as write_bar_parts


def publish(root, paths, dataset):
    import polars as pl
    manifest = build_manifest(root, run_id=paths[0].stem.removeprefix('part-'), dataset_id=dataset,
                              schema_version=dataset + '.v1', paths=paths,
                              row_count=sum(pl.read_parquet(p).height for p in paths),
                              quality_summary={'status': 'pass', 'finding_count': 0, 'findings': []})
    write_manifest(root, manifest)
    return manifest


def write_provider_bars(root, rows, **kwargs):
    paths = write_bar_parts(root, rows, **kwargs)
    publish(root, paths, 'provider_bars')
    return paths


def economic_row(row):
    return {'release_ts': None, 'asof_ts': row.get('ingest_ts'), 'frequency': None, 'units': None,
            'seasonal_adjustment': None, 'vintage_start': None, 'vintage_end': None,
            'availability_policy': 'release_date_unknown_ingest_asof', 'availability_lag_days': None,
            'source_hash': 'fixture', **row}


def write_economic_observations(root, rows, **kwargs):
    path = write_economic_part(root, [economic_row(row) for row in rows], **kwargs)
    publish(root, [path], 'economic_observations')
    return path
