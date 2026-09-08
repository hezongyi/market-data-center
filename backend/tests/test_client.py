from data_center.client import DataCenterClient


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"data": [{"symbol": "BTCUSDT"}], "errors": [], "meta": {}}


def test_client_reads_bars_with_provider(monkeypatch) -> None:
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, kwargs=kwargs)
        return FakeResponse()

    monkeypatch.setattr("data_center.client.requests.request", fake_request)
    rows = DataCenterClient("http://center", api_key="secret").bars(provider="fixture", symbol="BTCUSDT")
    assert rows == [{"symbol": "BTCUSDT"}]
    assert captured["url"] == "http://center/api/v1/bars"
    assert captured["kwargs"]["headers"]["X-API-Key"] == "secret"
    assert captured["kwargs"]["params"]["provider"] == "fixture"
