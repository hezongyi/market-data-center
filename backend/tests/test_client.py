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
    assert captured["kwargs"]["params"]["page_size"] == 1000


def test_client_follows_opaque_pagination(monkeypatch) -> None:
    calls = []

    class PageResponse(FakeResponse):
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    responses = iter([
        PageResponse({"data": [{"symbol": "ONE"}], "errors": [], "meta": {"next_cursor": "opaque"}}),
        PageResponse({"data": [{"symbol": "TWO"}], "errors": [], "meta": {"next_cursor": None}}),
    ])

    def fake_request(method, url, **kwargs):
        calls.append(kwargs["params"])
        return next(responses)

    monkeypatch.setattr("data_center.client.requests.request", fake_request)
    rows = DataCenterClient("http://center").bars(provider="fixture", symbol="TEST", page_size=1)
    assert rows == [{"symbol": "ONE"}, {"symbol": "TWO"}]
    assert calls == [
        {"provider": "fixture", "symbol": "TEST", "timeframe": "1d", "page_size": 1},
        {"provider": "fixture", "symbol": "TEST", "timeframe": "1d", "page_size": 1,
         "cursor": "opaque"},
    ]
