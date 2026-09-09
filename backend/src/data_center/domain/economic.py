from datetime import datetime

from pydantic import BaseModel


class EconomicObservation(BaseModel):
    series_id: str
    provider: str
    observation_date: str
    value: float | None
    release_ts: datetime | None = None
    asof_ts: datetime | None = None
    frequency: str
    units: str
    seasonal_adjustment: str
    vintage_start: str | None = None
    vintage_end: str | None = None
    availability_policy: str
    availability_lag_days: int | None = None
    ingest_ts: datetime
    source_hash: str
