from datetime import date

from data_center.dukascopy_migration import (
    attest_bid_provenance,
    compare_parity,
    plan_bounded_migration,
)


def item(**overrides):
    return {"asset_class": "fx", "symbol": "EURUSD", "timeframe": "1d", "year": 2026,
            "bytes": 4096, "price_types": ["raw"], "duplicate_timestamp_count": 0,
            "invalid_timestamp_count": 0, **overrides}


def plan(**kwargs):
    return plan_bounded_migration(
        item(**kwargs.pop("item", {})), start=date(2026, 1, 1), end=date(2026, 1, 10),
        capacity_free_ratio=0.20, bid_provenance_confirmed=True,
        source_reference="/legacy/part.parquet", **kwargs,
    )


def test_raw_legacy_is_rejected_even_when_caller_claims_provenance():
    assert plan().reason == "legacy_price_basis_not_bid"


def test_warning_capacity_rejects_even_short_window():
    result = plan_bounded_migration(item(price_types=["bid"]), start=date(2026, 1, 1), end=date(2026, 1, 10),
                                    capacity_free_ratio=0.12, bid_provenance_confirmed=True,
                                    source_reference="/legacy/part.parquet")
    assert result.reason == "capacity_not_ok"


def test_duplicate_and_long_window_are_rejected():
    result = plan_bounded_migration(item(price_types=["bid"], duplicate_timestamp_count=1),
                                    start=date(2026, 1, 1), end=date(2026, 2, 10),
                                    capacity_free_ratio=0.20, bid_provenance_confirmed=True,
                                    source_reference="/legacy/part.parquet")
    assert result.reason == "window_exceeds_31_days"
    result = plan_bounded_migration(item(price_types=["bid"], duplicate_timestamp_count=1),
                                    start=date(2026, 1, 1), end=date(2026, 1, 10),
                                    capacity_free_ratio=0.20, bid_provenance_confirmed=True,
                                    source_reference="/legacy/part.parquet")
    assert result.reason == "duplicate_timestamps"


def test_clean_confirmed_bid_item_is_ready_without_publication_side_effects():
    result = plan(item={"price_types": ["bid"]})
    assert result.status == "ready"
    assert result.reason == "all_migration_gates_pass"


def test_bid_attestation_is_explicit_and_hashed():
    attested = attest_bid_provenance(item(price_types=["bid"]), source_snapshot="a" * 64,
                                     attestation={"basis": "bid", "method": "provider_export_manifest",
                                                  "attested_by": "data-governance", "attested_at": "2026-09-11T00:00:00Z"})
    assert attested["basis"] == "bid" and len(attested["attestation_hash"]) == 64


def test_parity_compares_stable_ohlcv_and_snapshots():
    rows = [{"bar_ts": "2026-01-01T00:00:00Z", "open": 1, "high": 2, "low": 0.5,
             "close": 1.5, "volume": 3, "price_type": "bid"}]
    report = compare_parity(legacy_rows=rows, data_center_rows=list(reversed(rows)),
                           legacy_snapshot="a" * 64, data_center_snapshot="b" * 64)
    assert report["status"] == "pass" and report["snapshot_stable"] is True
