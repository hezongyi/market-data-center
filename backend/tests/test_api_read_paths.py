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
    assert bars.json()["meta"]["schema_versions"] == ["provider_bars.v1"]
    assert bars.json()["meta"]["snapshot_id"] and bars.json()["meta"]["next_cursor"] is None
    assert econ.status_code == 200 and econ.json()["meta"]["count"] == 1
    assert econ.json()["meta"]["schema_versions"] == ["economic_observations.v1"]
    assert econ.json()["meta"]["economic_schema_version"] == "v1"
    pit = client.get("/api/v1/economic/observations", params={"provider": "fred", "series_id": "TEST",
                                                              "mode": "pit", "asof_ts": "2026-01-03T00:00:00Z"})
    assert pit.status_code == 200 and pit.json()["meta"]["query_mode"] == "pit"
    assert client.get("/api/v1/economic/observations", params={"provider": "fred", "series_id": "TEST",
                                                                "mode": "pit"}).status_code == 422
    empty = client.get("/api/v1/economic/observations", params={"provider": "fred", "series_id": "MISSING"})
    assert empty.json()["meta"]["economic_schema_version"] == "unknown"
    assert empty.json()["meta"]["schema_versions"] == []


def test_api_economic_schema_version_comes_from_manifests(tmp_path) -> None:
    from storage_fixtures import publish

    from data_center.storage.economic import (
        write_economic_observations as write_economic_part,
    )

    root = tmp_path / "lake"
    v2_row = {
        "series_id": "V2", "provider": "fred", "observation_date": "2026-01-01",
        "release_ts": "2026-01-02T00:00:00+00:00", "asof_ts": "2026-01-02T00:00:00+00:00",
        "value": 1.0, "frequency": "monthly", "units": "index", "seasonal_adjustment": None,
        "vintage_start": "2026-01-02", "vintage_end": "9999-12-31",
        "availability_policy": "release_date_known", "availability_lag_days": 0,
        "ingest_ts": "2026-01-02T00:00:00+00:00", "source_hash": "v2",
        "source": "fred", "missing_reason": None,
    }
    v2_path = write_economic_part(root, [v2_row], part_id="v2-only")
    publish(root, [v2_path], "economic_observations", schema_version="economic_observations.v2")
    client = TestClient(create_app(Settings(canonical_root=root, ledger_path=tmp_path / "audit.sqlite")))
    v2 = client.get("/api/v1/economic/observations", params={"provider": "fred", "series_id": "V2"})
    assert v2.json()["meta"]["schema_versions"] == ["economic_observations.v2"]
    assert v2.json()["meta"]["economic_schema_version"] == "v2"

    write_economic_observations(root, [{
        "series_id": "V2", "provider": "fred", "observation_date": "2026-02-01", "value": 2.0,
        "vintage_start": "2026-02-01", "vintage_end": "9999-12-31",
        "ingest_ts": "2026-02-02T00:00:00+00:00", "asof_ts": "2026-02-02T00:00:00+00:00",
    }], part_id="legacy")
    mixed = client.get("/api/v1/economic/observations", params={"provider": "fred", "series_id": "V2"})
    assert mixed.json()["meta"]["schema_versions"] == [
        "economic_observations.v1", "economic_observations.v2",
    ]
    assert mixed.json()["meta"]["economic_schema_version"] == "mixed"
