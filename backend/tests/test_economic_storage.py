from data_center.storage.economic import write_economic_observations


def test_economic_observation_storage(tmp_path) -> None:
    path = write_economic_observations(tmp_path, [{"series_id": "TEST", "provider": "fixture", "observation_date": "2026-01-01", "value": 1.0, "ingest_ts": "2026-01-02T00:00:00+00:00"}])
    assert path.exists()
