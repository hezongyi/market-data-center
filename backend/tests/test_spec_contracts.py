from datetime import datetime, timezone

import polars as pl
import pytest
from data_center.domain.models import IngestJob, ProviderBar
from data_center.ingest.service import run_fixture_ingest
from data_center.storage.parquet import write_provider_bars
from data_center.storage.query import query_provider_bars


def bar(**values):
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ProviderBar(**({"symbol": "TEST", "asset_class": "crypto", "provider": "fixture", "timeframe": "1d",
                          "bar_ts": stamp, "ingest_ts": stamp, "open": 1, "high": 2, "low": 0, "close": 1,
                          "volume": 1, "source_hash": "test"} | values))


def test_quality_failure_blocks_publication(tmp_path, monkeypatch):
    class InvalidConnector:
        def fetch_bars(self, job):
            return [bar(volume=-1)]

    monkeypatch.setattr("data_center.ingest.service.get_connector", lambda provider: InvalidConnector())
    job = IngestJob(job_id="quality", symbol="TEST", start=bar().bar_ts, end=bar().bar_ts)
    with pytest.raises(ValueError, match="negative_volume"):
        run_fixture_ingest(job, tmp_path)
    assert not list(tmp_path.rglob("*.parquet"))


def test_duckdb_reads_mixed_legacy_timestamps_without_hive_columns(tmp_path):
    part = write_provider_bars(tmp_path, [bar()], part_id="typed")[0]
    legacy = bar(close=1.5, ingest_ts=datetime(2026, 1, 2, tzinfo=timezone.utc)).model_dump(mode="json")
    pl.DataFrame([legacy]).write_parquet(part.parent / "legacy.parquet")
    rows = query_provider_bars(tmp_path, provider="fixture", symbol="TEST", timeframe="1d",
                               start=bar().bar_ts, end=bar().bar_ts)
    assert len(rows) == 1
    assert rows[0]["close"] == 1.5
    assert rows[0]["bar_ts"] == bar().bar_ts
    assert "year" not in rows[0]
