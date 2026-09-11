"""Governed bounded migration planning for legacy Dukascopy data.

This module deliberately plans migration before publication. It refuses legacy
``raw`` data, duplicate timestamps, unbounded windows, and capacity-protected
operations; callers cannot use it to copy Parquet or manufacture manifests.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
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


def attest_bid_provenance(item: dict, *, source_snapshot: str, attestation: dict) -> dict:
    """Validate an explicit human/system attestation without changing source data."""
    required = {"basis", "method", "attested_by", "attested_at"}
    if set(attestation) < required or attestation.get("basis") != "bid":
        raise ValueError("BID provenance attestation is incomplete")
    if not source_snapshot or len(source_snapshot) != 64:
        raise ValueError("source snapshot hash is required")
    selector = {key: item.get(key) for key in ("asset_class", "symbol", "timeframe", "year")}
    payload = {"selector": selector, "source_snapshot": source_snapshot,
               "basis": "bid", "method": attestation["method"],
               "attested_by": attestation["attested_by"], "attested_at": attestation["attested_at"]}
    payload["attestation_hash"] = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return payload


def compare_parity(*, legacy_rows: list[dict], data_center_rows: list[dict],
                   legacy_snapshot: str, data_center_snapshot: str) -> dict:
    """Compare bounded rows using stable OHLCV identity, never ingesting or publishing."""
    fields = ("bar_ts", "open", "high", "low", "close", "volume", "price_type")

    def stable(rows):
        normalized = [{key: row.get(key) for key in fields} for row in rows]
        return sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    legacy_hash, data_center_hash = stable(legacy_rows), stable(data_center_rows)
    return {
        "status": "pass" if len(legacy_rows) == len(data_center_rows) and legacy_hash == data_center_hash else "failed",
        "legacy_row_count": len(legacy_rows), "data_center_row_count": len(data_center_rows),
        "legacy_hash": legacy_hash, "data_center_hash": data_center_hash,
        "legacy_snapshot": legacy_snapshot, "data_center_snapshot": data_center_snapshot,
        "snapshot_stable": bool(legacy_snapshot and data_center_snapshot),
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
