from data_center.connectors.base import MarketConnector
from data_center.connectors.fixture import fetch_bars


class FixtureConnector:
    provider = "fixture"

    def fetch_bars(self, job):
        return fetch_bars(job)


CONNECTORS: dict[str, MarketConnector] = {"fixture": FixtureConnector()}


def get_connector(provider: str) -> MarketConnector:
    try:
        return CONNECTORS[provider]
    except KeyError as exc:
        raise ValueError(f"unsupported provider: {provider}") from exc

