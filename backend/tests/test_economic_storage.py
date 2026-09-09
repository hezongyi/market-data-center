import pytest
from storage_fixtures import economic_row, write_economic_observations

from data_center.storage.query import (
    economic_observations_coverage,
    query_economic_observations,
)


def test_economic_observation_storage(tmp_path) -> None:
    path = write_economic_observations(tmp_path, [{"series_id": "TEST", "provider": "fixture", "observation_date": "2026-01-01", "value": 1.0, "ingest_ts": "2026-01-02T00:00:00+00:00"}])
    assert path.exists()


def test_economic_query_selects_latest_vintage_without_overwriting_parts(tmp_path) -> None:
    common = {"series_id": "TEST", "provider": "fred", "observation_date": "2026-01-01", "ingest_ts": "2026-01-02T00:00:00+00:00", "asof_ts": "2026-01-02T00:00:00+00:00"}
    first = write_economic_observations(tmp_path, [{**common, "value": 1.0, "vintage_start": "2026-01-01", "vintage_end": "2026-01-15"}], part_id="first")
    second = write_economic_observations(tmp_path, [{**common, "value": 2.0, "vintage_start": "2026-01-16", "vintage_end": "9999-12-31"}], part_id="second")
    assert first != second
    rows = query_economic_observations(tmp_path, provider="fred", series_id="TEST")
    assert rows == [economic_row({**common, "value": 2.0, "vintage_start": "2026-01-16", "vintage_end": "9999-12-31"})]
    coverage = economic_observations_coverage(tmp_path, provider="fred", series_id="TEST")
    assert coverage["row_count"] == 1
    assert coverage["min_date"] == "2026-01-01"


def test_unknown_old_vintage_does_not_override_known_current_vintage(tmp_path):
    common = {"series_id": "TEST", "provider": "fred", "observation_date": "2026-06-01"}
    write_economic_observations(tmp_path, [{**common, "value": 1.0, "vintage_start": None,
        "ingest_ts": "2026-07-12T00:00:00+00:00", "asof_ts": "2026-07-12T00:00:00+00:00"}], part_id="unknown")
    write_economic_observations(tmp_path, [{**common, "value": 2.0, "vintage_start": "2026-09-09",
        "ingest_ts": "2026-09-09T08:00:00+00:00", "asof_ts": "2026-09-09T08:00:00+00:00"}], part_id="known")
    rows = query_economic_observations(tmp_path, provider="fred", series_id="TEST")
    assert len(rows) == 1 and rows[0]["value"] == 2.0


def test_pit_query_excludes_unknown_release_and_selects_visible_vintage(tmp_path):
    common = {"series_id": "TEST", "provider": "fred", "observation_date": "2026-06-01",
              "ingest_ts": "2026-07-12T00:00:00+00:00", "asof_ts": "2026-07-12T00:00:00+00:00"}
    write_economic_observations(tmp_path, [{**common, "value": 1.0, "release_ts": None,
        "availability_policy": "release_date_unknown_ingest_asof", "vintage_start": None}], part_id="unknown")
    write_economic_observations(tmp_path, [{**common, "value": 2.0, "release_ts": "2026-07-10T12:00:00+00:00",
        "availability_policy": "release_date_known", "vintage_start": None}], part_id="known")
    assert query_economic_observations(tmp_path, provider="fred", series_id="TEST", asof_ts="2026-07-11T00:00:00+00:00") == []
    rows = query_economic_observations(tmp_path, provider="fred", series_id="TEST", asof_ts="2026-07-12T00:00:00+00:00")
    assert len(rows) == 1 and rows[0]["value"] == 2.0


def test_economic_writer_refuses_to_overwrite_immutable_part(tmp_path):
    row = {"series_id": "TEST", "provider": "fred", "observation_date": "2026-01-01", "value": 1.0,
           "ingest_ts": "2026-01-02T00:00:00+00:00"}
    write_economic_observations(tmp_path, [row], part_id="immutable")
    with pytest.raises(FileExistsError, match="immutable part"):
        write_economic_observations(tmp_path, [row], part_id="immutable")
