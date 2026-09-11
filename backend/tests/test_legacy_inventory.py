import json
from pathlib import Path

import polars as pl

from data_center.capacity import CapacityPolicy
from data_center.legacy_inventory import inventory_legacy_dukascopy


def test_legacy_inventory_is_read_only_and_reports_governance_fields(tmp_path):
    root = (tmp_path / "provider=dukascopy" / "asset_class=fx" / "symbol=EURUSD" /
            "timeframe=1m" / "year=2026")
    root.mkdir(parents=True)
    part = root / "legacy.parquet"
    pl.DataFrame({
        "asset_class": ["fx"] * 4, "symbol": ["EURUSD"] * 4, "timeframe": ["1m"] * 4,
        "year": [2026] * 4,
        "bar_ts": ["2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z",
                   "2026-01-01T00:01:00Z", "2026-01-01T00:03:00Z"],
        "price_type": ["bid"] * 4,
    }).write_parquet(part)
    original = part.read_bytes()

    report = inventory_legacy_dukascopy(
        tmp_path / "provider=dukascopy", tmp_path / "evidence",
        CapacityPolicy(warning_free_ratio=1.0, critical_free_ratio=0.0),
    )

    assert report["result"] == "pass"
    assert report["details"]["source_mutated"] is False
    assert report["details"]["bulk_migration_allowed"] is False
    assert part.read_bytes() == original
    group = report["details"]["groups"][0]
    assert group["file_count"] == 1 and group["row_count"] == 4
    assert group["price_types"] == ["bid"]
    assert group["duplicate_timestamp_count"] == 1
    assert group["coverage_gap_count"] == 1
    receipt = json.loads(Path(report["receipt"]).read_text())
    assert receipt["details"]["inventory_mode"] == "read_only_no_manifest_publication"


def test_legacy_inventory_excludes_manifest_visible_parts(tmp_path):
    root = (tmp_path / "provider_bars" / "provider=dukascopy" / "asset_class=fx" /
            "symbol=EURUSD" / "timeframe=1d" / "year=2026")
    root.mkdir(parents=True)
    columns = {"asset_class": ["fx"], "symbol": ["EURUSD"], "timeframe": ["1d"], "year": [2026],
               "bar_ts": ["2026-01-01T00:00:00Z"], "price_type": ["raw"]}
    legacy = root / "legacy.parquet"
    published = root / "part-published.parquet"
    pl.DataFrame(columns).write_parquet(legacy)
    pl.DataFrame({**columns, "price_type": ["bid"]}).write_parquet(published)
    manifests = tmp_path / ".manifests"
    manifests.mkdir()
    (manifests / "published.json").write_text(json.dumps({
        "parts": [{"path": str(published.relative_to(tmp_path))}],
    }))

    report = inventory_legacy_dukascopy(
        tmp_path / "provider_bars/provider=dukascopy", tmp_path / "evidence",
        canonical_root=tmp_path,
    )

    assert report["details"]["manifest_visible_files_excluded"] == 1
    assert report["details"]["file_count"] == 1
    assert report["details"]["groups"][0]["price_types"] == ["raw"]
