from datetime import datetime, timezone

from data_center.domain.models import ProviderBar
from data_center.storage.parquet import write_provider_bars
from data_center.storage.query import query_provider_bars
from data_center.storage.query import provider_bars_coverage


def _bar(*, bar_ts: datetime, close: float, ingest_ts: datetime) -> ProviderBar:
    return ProviderBar(symbol="BTCUSDT", asset_class="crypto", provider="fixture", timeframe="1d", bar_ts=bar_ts, open=close - 1, high=close + 1, low=close - 2, close=close, volume=1.0, ingest_ts=ingest_ts, source_hash=f"hash-{close}")


def test_provider_bars_are_year_partitioned_and_current_state_is_deduplicated(tmp_path) -> None:
    original = _bar(bar_ts=datetime(2025, 12, 31, tzinfo=timezone.utc), close=100.0, ingest_ts=datetime(2026, 1, 1, tzinfo=timezone.utc))
    next_year = _bar(bar_ts=datetime(2026, 1, 1, tzinfo=timezone.utc), close=101.0, ingest_ts=datetime(2026, 1, 2, tzinfo=timezone.utc))
    replacement = _bar(bar_ts=original.bar_ts, close=102.0, ingest_ts=datetime(2026, 1, 3, tzinfo=timezone.utc))
    paths = write_provider_bars(tmp_path, [original, next_year], part_id="first")
    write_provider_bars(tmp_path, [replacement], part_id="second")
    assert {path.parent.name for path in paths} == {"year=2025", "year=2026"}
    rows = query_provider_bars(tmp_path, provider="fixture", symbol="BTCUSDT", timeframe="1d")
    assert [row["close"] for row in rows] == [102.0, 101.0]
    coverage = provider_bars_coverage(tmp_path, provider="fixture", symbol="BTCUSDT", timeframe="1d")
    assert coverage["row_count"] == 2
    assert coverage["min_ts"] == original.bar_ts
    assert coverage["max_ts"] == next_year.bar_ts
