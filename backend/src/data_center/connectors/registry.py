from data_center.connectors.base import MarketConnector
from data_center.connectors.binance import BinanceConnector
from data_center.connectors.dukascopy import DukascopyConnector
from data_center.connectors.fixture import fetch_bars
from data_center.connectors.fred import FredConnector
from data_center.connectors.yfinance import YFinanceConnector


class FixtureConnector:
    provider = "fixture"
    end_inclusive = True

    def fetch_bars(self, job):
        return fetch_bars(job)


CONNECTORS: dict[str, MarketConnector] = {
    "fixture": FixtureConnector(), "yfinance": YFinanceConnector(),
    "binance": BinanceConnector(), "dukascopy": DukascopyConnector(),
}

ECONOMIC_CONNECTORS = {"fred": FredConnector()}


def get_connector(provider: str) -> MarketConnector:
    from data_center.settings import Settings

    if not Settings().provider_allowed(provider):
        raise ValueError(f"provider disabled in this environment: {provider}")
    try:
        return CONNECTORS[provider]
    except KeyError as exc:
        raise ValueError(f"unsupported provider: {provider}") from exc
