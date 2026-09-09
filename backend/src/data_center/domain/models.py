from datetime import datetime

from pydantic import BaseModel


class ProviderBar(BaseModel):
    symbol: str
    asset_class: str
    provider: str
    timeframe: str
    bar_ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    currency: str = "USD"
    price_type: str = "raw"
    ingest_ts: datetime
    source_hash: str


class IngestJob(BaseModel):
    job_id: str
    dataset_id: str = "provider_bars"
    provider: str = "fixture"
    symbol: str
    asset_class: str = "crypto"
    timeframe: str = "1d"
    start: datetime
    end: datetime
