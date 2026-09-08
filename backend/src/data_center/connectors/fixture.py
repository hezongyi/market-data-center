from datetime import datetime, timedelta, timezone
import hashlib

from data_center.domain.models import IngestJob, ProviderBar


def fetch_bars(job: IngestJob) -> list[ProviderBar]:
    rows: list[ProviderBar] = []
    cursor = job.start
    index = 0
    while cursor <= job.end:
        close = 100.0 + index
        rows.append(ProviderBar(
            symbol=job.symbol, asset_class=job.asset_class, provider=job.provider,
            timeframe=job.timeframe, bar_ts=cursor, open=close - 1, high=close + 1,
            low=close - 2, close=close, volume=1000 + index,
            ingest_ts=datetime.now(timezone.utc),
            source_hash=hashlib.sha256(f"{job.symbol}:{cursor.isoformat()}".encode()).hexdigest(),
        ))
        cursor += timedelta(days=1)
        index += 1
    return rows

