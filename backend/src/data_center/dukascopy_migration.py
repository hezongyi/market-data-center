"""Governed bounded migration planning for legacy Dukascopy data.

This module deliberately plans migration before publication. It refuses legacy
``raw`` data, duplicate timestamps, unbounded windows, and capacity-protected
operations; callers cannot use it to copy Parquet or manufacture manifests.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from data_center.capacity import CapacityPolicy


@dataclass(frozen=True)
class MigrationDecision:
    status: str
    reason: str
    selector: dict
    window: dict
    estimated_bytes: int
    source_reference: str

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "selector": self.selector,
            "window": self.window,
            "estimated_bytes": self.estimated_bytes,
            "source_reference": self.source_reference,
        }


def plan_bounded_migration(item: dict, *, start: date, end: date,
                           capacity_free_ratio: float | None,
                           bid_provenance_confirmed: bool,
                           source_reference: str | Path) -> MigrationDecision:
    """Return an explicit migration decision without reading or writing data."""
    selector = {key: item.get(key) for key in ("asset_class", "symbol", "timeframe", "year")}
    window = {"start": start.isoformat(), "end": end.isoformat(), "semantics": "half-open"}
    estimated_bytes = int(item.get("bytes", 0) or 0)
    source = str(Path(source_reference))
    if end <= start:
        return MigrationDecision("rejected", "invalid_window", selector, window, estimated_bytes, source)
    days = (end - start).days
    if days > 31:
        return MigrationDecision("rejected", "window_exceeds_31_days", selector, window, estimated_bytes, source)
    if (capacity_free_ratio is not None and
            CapacityPolicy().classify(capacity_free_ratio) != "ok"):
        return MigrationDecision("rejected", "capacity_not_ok", selector, window, estimated_bytes, source)
    if not bid_provenance_confirmed:
        return MigrationDecision("rejected", "bid_provenance_unconfirmed", selector, window, estimated_bytes, source)
    price_types = set(item.get("price_types") or [])
    if price_types != {"bid"}:
        return MigrationDecision("rejected", "legacy_price_basis_not_bid", selector, window, estimated_bytes, source)
    if int(item.get("duplicate_timestamp_count", 0) or 0) != 0:
        return MigrationDecision("rejected", "duplicate_timestamps", selector, window, estimated_bytes, source)
    if int(item.get("invalid_timestamp_count", 0) or 0) != 0:
        return MigrationDecision("rejected", "invalid_timestamps", selector, window, estimated_bytes, source)
    return MigrationDecision("ready", "all_migration_gates_pass", selector, window, estimated_bytes, source)
