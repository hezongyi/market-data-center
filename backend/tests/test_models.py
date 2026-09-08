from datetime import datetime, timezone

from data_center.domain.models import IngestJob


def test_ingest_job_defaults() -> None:
    job = IngestJob(job_id="x", symbol="BTCUSDT", start=datetime.now(timezone.utc), end=datetime.now(timezone.utc))
    assert job.dataset_id == "provider_bars"
    assert job.timeframe == "1d"

