from datetime import datetime
from pydantic import BaseModel


class EconomicObservation(BaseModel):
    series_id: str
    provider: str
    observation_date: str
    value: float | None
    ingest_ts: datetime

