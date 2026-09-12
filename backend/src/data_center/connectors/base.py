from typing import Protocol

from data_center.domain.models import IngestJob, ProviderBar


class MarketConnector(Protocol):
    provider: str

    def fetch_bars(self, job: IngestJob) -> list[ProviderBar]: ...
