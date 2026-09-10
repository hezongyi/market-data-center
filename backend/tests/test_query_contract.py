import json
from datetime import datetime, timezone

import polars as pl
import pytest
from storage_fixtures import publish, write_economic_observations, write_provider_bars

from data_center.catalog.manifest import PublicationError, manifest_path
from data_center.catalog.snapshot import Catalog
from data_center.domain.models import ProviderBar
from data_center.storage.economic import (
    write_economic_observations as write_economic_part,
)
from data_center.storage.query import CursorError, QueryEngine, QueryValidationError


def bar(day: int, *, ingest_day: int | None = None) -> ProviderBar:
    ts = datetime(2026, 1, day, tzinfo=timezone.utc)
    return ProviderBar(symbol="TEST", asset_class="equity", provider="fixture", timeframe="1d",
                       bar_ts=ts, open=day, high=day + 1, low=day - 1, close=day + 0.5, volume=day,
                       ingest_ts=datetime(2026, 2, ingest_day or day, tzinfo=timezone.utc),
                       source_hash=f"hash-{day}-{ingest_day}")


def economic(day: int, *, source: str | None = None) -> dict:
    row = {"series_id": "TEST", "provider": "fred", "observation_date": f"2026-01-{day:02d}",
           "value": float(day), "vintage_start": f"2026-01-{day:02d}", "vintage_end": "9999-12-31",
           "ingest_ts": f"2026-02-{day:02d}T00:00:00+00:00",
           "asof_ts": f"2026-02-{day:02d}T00:00:00+00:00"}
    if source is not None:
        row.update(source=source, missing_reason=None)
    return row


def test_schema_provenance_v1_v2_mixed_and_empty(tmp_path):
    root = tmp_path / "lake"
    write_economic_observations(root, [economic(1)], part_id="v1")
    engine = QueryEngine(root)
    assert engine.economic_observations_page(provider="fred", series_id="TEST").schema_versions == [
        "economic_observations.v1"
    ]

    v2_path = write_economic_part(root, [
        {"release_ts": None, "frequency": None, "units": None, "seasonal_adjustment": None,
         "availability_policy": "realtime_vintage", "availability_lag_days": 0,
         "source_hash": "fixture", **economic(2, source="fred")}
    ], part_id="v2")
    publish(root, [v2_path], "economic_observations", schema_version="economic_observations.v2")
    mixed = engine.economic_observations_page(provider="fred", series_id="TEST")
    assert mixed.schema_versions == ["economic_observations.v1", "economic_observations.v2"]
    assert QueryEngine(root).economic_observations_page(provider="fred", series_id="MISSING").schema_versions == []


def test_catalog_query_avoids_publication_time_row_validation(tmp_path, monkeypatch):
    root = tmp_path / "lake"
    write_provider_bars(root, [bar(1), bar(2)], part_id="bars")
    monkeypatch.setattr(pl, "read_parquet", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("row read")))
    page = QueryEngine(root).provider_bars_page(provider="fixture", symbol="TEST", timeframe="1d")
    assert page.count == 2


def test_catalog_refresh_restart_and_corruption_fail_closed(tmp_path):
    root = tmp_path / "lake"
    write_provider_bars(root, [bar(1)], part_id="one")
    catalog = Catalog(root)
    first = catalog.resolve("provider_bars", {"provider": "fixture", "symbol": "TEST", "timeframe": "1d"})
    assert catalog.refresh_count == 1
    assert catalog.resolve("provider_bars", {"provider": "fixture", "symbol": "TEST",
                                             "timeframe": "1d"}).snapshot_id == first.snapshot_id
    assert catalog.cache_hits == 1
    write_provider_bars(root, [bar(2)], part_id="two")
    second = catalog.resolve("provider_bars", {"provider": "fixture", "symbol": "TEST", "timeframe": "1d"})
    assert second.snapshot_id != first.snapshot_id and len(second.parts) == 2
    assert Catalog(root).resolve("provider_bars", {"provider": "fixture", "symbol": "TEST",
                                                   "timeframe": "1d"}).snapshot_id == second.snapshot_id
    damaged = manifest_path(root, "two")
    payload = json.loads(damaged.read_text())
    payload["status"] = "staging"
    damaged.write_text(json.dumps(payload))
    with pytest.raises(PublicationError):
        Catalog(root).resolve("provider_bars", {"provider": "fixture", "symbol": "TEST", "timeframe": "1d"})


def test_snapshot_bound_pagination_survives_concurrent_publication(tmp_path):
    root = tmp_path / "lake"
    write_provider_bars(root, [bar(day) for day in range(1, 6)], part_id="initial")
    engine = QueryEngine(root)
    first = engine.provider_bars_page(provider="fixture", symbol="TEST", timeframe="1d", page_size=2)
    assert [row["bar_ts"].day for row in first.rows] == [1, 2]
    write_provider_bars(root, [bar(6)], part_id="new")
    rows = list(first.rows)
    cursor = first.next_cursor
    while cursor:
        page = engine.provider_bars_page(provider="fixture", symbol="TEST", timeframe="1d",
                                         page_size=2, cursor=cursor)
        rows.extend(page.rows)
        cursor = page.next_cursor
    assert [row["bar_ts"].day for row in rows] == [1, 2, 3, 4, 5]
    fresh = engine.provider_bars_page(provider="fixture", symbol="TEST", timeframe="1d", page_size=10)
    assert [row["bar_ts"].day for row in fresh.rows] == [1, 2, 3, 4, 5, 6]


def test_cursor_and_page_size_errors_are_stable(tmp_path):
    root = tmp_path / "lake"
    write_provider_bars(root, [bar(1), bar(2)], part_id="bars")
    engine = QueryEngine(root)
    first = engine.provider_bars_page(provider="fixture", symbol="TEST", timeframe="1d", page_size=1)
    with pytest.raises(CursorError, match="does not match"):
        engine.provider_bars_page(provider="fixture", symbol="OTHER", timeframe="1d",
                                  page_size=1, cursor=first.next_cursor)
    with pytest.raises(CursorError, match="tampered"):
        engine.provider_bars_page(provider="fixture", symbol="TEST", timeframe="1d",
                                  page_size=1, cursor=first.next_cursor[:-1] + "x")
    with pytest.raises(QueryValidationError, match="between 1 and 10000"):
        engine.provider_bars_page(provider="fixture", symbol="TEST", timeframe="1d", page_size=10_001)
    assert engine.metrics.snapshot(engine.catalog)["rejected_oversized_query_total"] == 1


def test_pit_visibility_is_stable_across_pages(tmp_path):
    root = tmp_path / "lake"
    rows = []
    for day in range(1, 5):
        row = economic(day)
        row.update(release_ts=f"2026-02-{day:02d}T00:00:00+00:00",
                   availability_policy="release_date_known", availability_lag_days=0)
        rows.append(row)
    write_economic_observations(root, rows, part_id="pit")
    engine = QueryEngine(root)
    page = engine.economic_observations_page(provider="fred", series_id="TEST", mode="pit",
                                             asof_ts="2026-02-02T12:00:00+00:00", page_size=1)
    visible = list(page.rows)
    while page.next_cursor:
        page = engine.economic_observations_page(provider="fred", series_id="TEST", mode="pit",
                                                 asof_ts="2026-02-02T12:00:00+00:00", page_size=1,
                                                 cursor=page.next_cursor)
        visible.extend(page.rows)
    assert [row["observation_date"] for row in visible] == ["2026-01-01", "2026-01-02"]
