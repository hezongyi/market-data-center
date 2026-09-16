from datetime import datetime, timedelta, timezone

from data_center.connectors.preview_fixture import PreviewFixtureConnector
from data_center.domain.models import IngestJob


def test_preview_fixture_preserves_dukascopy_identity_and_minute_coverage() -> None:
    start = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    job = IngestJob(
        job_id="preview-eurusd", dataset_id="provider_bars",
        provider="dukascopy", symbol="EURUSD", asset_class="fx", timeframe="1m",
        start=start, end=start + timedelta(minutes=10),
        run_scope="production", run_kind="ingest",
    )
    rows = PreviewFixtureConnector().fetch_bars(job)
    assert len(rows) == 10
    assert [row.bar_ts for row in rows] == [start + timedelta(minutes=i) for i in range(10)]
    assert {(row.provider, row.symbol, row.timeframe, row.price_type) for row in rows} == {
        ("dukascopy", "EURUSD", "1m", "bid")
    }
    assert PreviewFixtureConnector().fetch_bars(job)[0].close == rows[0].close


def test_preview_fixture_respects_dukascopy_closed_session() -> None:
    start = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)  # Saturday
    job = IngestJob(
        job_id="preview-weekend", dataset_id="provider_bars",
        provider="dukascopy", symbol="EURUSD", asset_class="fx", timeframe="1m",
        start=start, end=start + timedelta(hours=1),
        run_scope="production", run_kind="ingest",
    )
    assert PreviewFixtureConnector().fetch_bars(job) == []
