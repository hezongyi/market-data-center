from datetime import datetime
from pydantic import BaseModel


class EconomicObservation(BaseModel):
    series_id: str
    provider: str
    observation_date: str
    value: float | None
    ingest_ts: datetime
    release_ts: datetime | None = None
    asof_ts: datetime | None = None
    availability_policy: str = "provider_release"
    vintage_start: str | None = None
    vintage_end: str | None = None
