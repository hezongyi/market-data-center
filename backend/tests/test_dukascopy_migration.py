from datetime import date

from data_center.dukascopy_migration import plan_bounded_migration


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
