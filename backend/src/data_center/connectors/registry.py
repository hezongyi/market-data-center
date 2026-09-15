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

    settings = Settings()
    if not settings.provider_allowed(provider):
        raise ValueError(f"provider disabled in this environment: {provider}")
    if settings.data_mode == "fixture" and provider != "fixture":
        if settings.deployment_manifest:
            raise ValueError("fixture connector substitution is not allowed in a deployment")
        from data_center.connectors.preview_fixture import PreviewFixtureConnector

        return PreviewFixtureConnector()
    if settings.data_mode == "live":
        if settings.deployment_manifest or provider != "dukascopy":
            raise ValueError("live preview only allows the bounded Dukascopy connector")
        from data_center.connectors.bounded_live import BoundedLiveConnector

        return BoundedLiveConnector(CONNECTORS[provider], settings)
    try:
        return CONNECTORS[provider]
    except KeyError as exc:
        raise ValueError(f"unsupported provider: {provider}") from exc
