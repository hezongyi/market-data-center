from datetime import datetime, timezone

from fastapi.testclient import TestClient
from storage_fixtures import write_economic_observations, write_provider_bars

from data_center.api.app import create_app
from data_center.domain.models import ProviderBar
from data_center.settings import Settings


def test_api_reads_canonical_bars_and_economic_observations(tmp_path) -> None:
    root = tmp_path / "lake"
    write_provider_bars(root, [ProviderBar(symbol="BTCUSDT", asset_class="crypto", provider="fixture", timeframe="1d", bar_ts=datetime(2026, 1, 1, tzinfo=timezone.utc), open=1, high=2, low=0, close=1.5, volume=2, ingest_ts=datetime(2026, 1, 2, tzinfo=timezone.utc), source_hash="x")], part_id="bars")
    write_economic_observations(root, [{"series_id": "TEST", "provider": "fred", "observation_date": "2026-01-01", "value": 1.0, "vintage_start": "2026-01-01", "vintage_end": "9999-12-31", "ingest_ts": "2026-01-02T00:00:00+00:00", "asof_ts": "2026-01-02T00:00:00+00:00"}], part_id="econ")
    client = TestClient(create_app(Settings(canonical_root=root, ledger_path=tmp_path / "audit.sqlite")))
    bars = client.get("/api/v1/bars", params={"provider": "fixture", "symbol": "BTCUSDT"})
    econ = client.get("/api/v1/economic/observations", params={"provider": "fred", "series_id": "TEST"})
    assert bars.status_code == 200 and bars.json()["meta"]["count"] == 1
    assert econ.status_code == 200 and econ.json()["meta"]["count"] == 1
