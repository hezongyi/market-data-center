from data_center.connectors.fred import FredConnector


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_fred_connector_normalizes_metadata_and_realtime_vintage(monkeypatch) -> None:
    def fake_get(url, **kwargs):
        if url.endswith("/observations"):
            return FakeResponse({"observations": [{"date": "2026-01-01", "value": "123.4", "realtime_start": "2026-01-02", "realtime_end": "9999-12-31"}]})
        return FakeResponse({"seriess": [{"frequency": "Monthly", "units": "Index", "seasonal_adjustment": "Seasonally Adjusted"}]})

    monkeypatch.setattr("data_center.connectors.fred.requests.get", fake_get)
    rows = FredConnector(api_key="test").fetch_observations("TEST")
    assert rows[0]["availability_policy"] == "realtime_vintage"
    assert rows[0]["frequency"] == "Monthly"
    assert rows[0]["units"] == "Index"
    assert rows[0]["seasonal_adjustment"] == "Seasonally Adjusted"
