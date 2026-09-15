from datetime import datetime, timedelta, timezone

import pytest

from data_center.connectors.bounded_live import BoundedLiveConnector
from data_center.domain.models import IngestJob
from data_center.settings import Settings


class RecordingConnector:
    provider = "dukascopy"
    version = "recording"

    def __init__(self):
        self.jobs = []

    def fetch_bars(self, job):
        self.jobs.append(job)
        return []


def settings(tmp_path, *, budget=1):
    return Settings(
        canonical_root=tmp_path / "canonical",
        data_mode="live", preview_symbols="EURUSD",
        preview_live_start="2026-09-14T00:00:00Z",
        preview_live_end="2026-09-15T00:00:00Z",
        preview_live_request_budget=budget,
        preview_live_byte_budget=1024,
        preview_live_budget_path=tmp_path / "live-budget.json",
    )


def job(*, symbol="EURUSD", start=None, end=None):
    lower = datetime(2026, 9, 14, tzinfo=timezone.utc)
    return IngestJob(
        job_id="live", dataset_id="provider_bars", provider="dukascopy",
        symbol=symbol, asset_class="fx", timeframe="1m",
        start=start or lower, end=end or lower + timedelta(hours=1),
        run_scope="production", run_kind="ingest",
    )


def test_live_connector_enforces_scope_before_contacting_provider(tmp_path):
    real = RecordingConnector()
    connector = BoundedLiveConnector(real, settings(tmp_path))
    with pytest.raises(ValueError, match="approved scope"):
        connector.fetch_bars(job(symbol="GBPUSD"))
    with pytest.raises(ValueError, match="approved UTC window"):
        connector.fetch_bars(job(end=datetime(2026, 9, 15, 1, tzinfo=timezone.utc)))
    assert real.jobs == []


def test_live_connector_spends_each_request_once_and_stops_at_budget(tmp_path):
    real = RecordingConnector()
    connector = BoundedLiveConnector(real, settings(tmp_path))
    assert connector.fetch_bars(job()) == []
    with pytest.raises(ValueError, match="request budget exhausted"):
        connector.fetch_bars(job())
    assert len(real.jobs) == 1
