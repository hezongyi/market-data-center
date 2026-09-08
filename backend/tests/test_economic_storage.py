from data_center.storage.economic import write_economic_observations
from data_center.storage.query import query_economic_observations


def test_economic_observation_storage(tmp_path) -> None:
    path = write_economic_observations(tmp_path, [{"series_id": "TEST", "provider": "fixture", "observation_date": "2026-01-01", "value": 1.0, "ingest_ts": "2026-01-02T00:00:00+00:00"}])
    assert path.exists()


def test_economic_query_selects_latest_vintage_without_overwriting_parts(tmp_path) -> None:
    common = {"series_id": "TEST", "provider": "fred", "observation_date": "2026-01-01", "ingest_ts": "2026-01-02T00:00:00+00:00", "asof_ts": "2026-01-02T00:00:00+00:00"}
    first = write_economic_observations(tmp_path, [{**common, "value": 1.0, "vintage_start": "2026-01-01", "vintage_end": "2026-01-15"}], part_id="first")
    second = write_economic_observations(tmp_path, [{**common, "value": 2.0, "vintage_start": "2026-01-16", "vintage_end": "9999-12-31"}], part_id="second")
    assert first != second
    rows = query_economic_observations(tmp_path, provider="fred", series_id="TEST")
    assert rows == [{**common, "value": 2.0, "vintage_start": "2026-01-16", "vintage_end": "9999-12-31"}]
