from datetime import datetime, timezone

import pandas as pd
import pytest

from data_center.connectors.yfinance import YFinanceConnector
from data_center.domain.models import IngestJob


def job():
    return IngestJob(job_id="proxy-test", symbol="SPY", provider="yfinance",
                     asset_class="etf", start=datetime(2026, 8, 26, tzinfo=timezone.utc),
                     end=datetime(2026, 9, 9, tzinfo=timezone.utc))


@pytest.mark.parametrize("override", [None, "http://provider.example:7890"])
def test_yfinance_proxy_session_normalizes_multiindex(monkeypatch, override):
    monkeypatch.setenv("DATACENTER_PROXY_URL", "http://shared.example:7890")
    monkeypatch.delenv("YFINANCE_PROXY_URL", raising=False)
    if override:
        monkeypatch.setenv("YFINANCE_PROXY_URL", override)

    def download(symbol, **kwargs):
        assert kwargs["session"].proxies["https"] == (override or "http://shared.example:7890")
        assert kwargs["auto_adjust"] is False
        assert kwargs["interval"] == "1d"
        return pd.DataFrame([[100, 102, 99, 101, 1234]],
                            columns=pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Volume"], [symbol]]),
                            index=pd.to_datetime(["2026-09-08"]))

    monkeypatch.setattr("yfinance.download", download)
    rows = YFinanceConnector().fetch_bars(job())
    assert len(rows) == 1
    assert rows[0].close == 101
    assert rows[0].bar_ts == datetime(2026, 9, 8, tzinfo=timezone.utc)


def test_yfinance_empty_result_is_not_success(monkeypatch):
    monkeypatch.setattr("yfinance.download", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(YFinanceConnector, "_chart_frame", staticmethod(lambda *args: pd.DataFrame()))
    with pytest.raises(ValueError, match="no bars"):
        YFinanceConnector().fetch_bars(job())


def test_yfinance_empty_download_falls_back_through_proxy(monkeypatch):
    monkeypatch.setenv("DATACENTER_PROXY_URL", "http://shared.example:7890")
    monkeypatch.delenv("YFINANCE_PROXY_URL", raising=False)
    monkeypatch.setattr("yfinance.download", lambda *args, **kwargs: pd.DataFrame())

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"chart": {"result": [{
                "meta": {"exchangeTimezoneName": "America/New_York"},
                "timestamp": [int(datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc).timestamp())],
                "indicators": {"quote": [{"open": [100], "high": [102], "low": [99],
                                          "close": [101], "volume": [1200]}]}}]}}

    def get(session, url, **kwargs):
        assert session.proxies["https"] == "http://shared.example:7890"
        assert url.endswith("/SPY")
        assert kwargs["params"]["interval"] == "1d"
        return Response()

    monkeypatch.setattr("curl_cffi.requests.Session.get", get)
    rows = YFinanceConnector().fetch_bars(job())
    assert rows[0].bar_ts == datetime(2026, 9, 8, tzinfo=timezone.utc)
    assert rows[0].close == 101
