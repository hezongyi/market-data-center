"""Shared disk-capacity policy used by API, workers, monitoring, and operations."""
from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class CapacitySnapshot:
    total_bytes: int
    used_bytes: int
    free_bytes: int
    free_ratio: float | None
    status: str
    warning_free_ratio: float
    critical_free_ratio: float

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CapacityPolicy:
    warning_free_ratio: float = 0.15
    critical_free_ratio: float = 0.10

    def __post_init__(self) -> None:
        if not 0 <= self.critical_free_ratio < self.warning_free_ratio <= 1:
            raise ValueError("capacity ratios require 0 <= critical < warning <= 1")

    def classify(self, free_ratio: float | None) -> str:
        if free_ratio is None:
            return "unknown"
        if free_ratio < self.critical_free_ratio:
            return "critical"
        if free_ratio < self.warning_free_ratio:
            return "warning"
        return "ok"

    def inspect(self, path: Path) -> CapacitySnapshot:
        path = Path(path)
        probe = path if path.exists() else next((parent for parent in path.parents if parent.exists()), path.parent)
        usage = shutil.disk_usage(probe)
        free_ratio = usage.free / usage.total if usage.total else None
        return CapacitySnapshot(
            total_bytes=usage.total,
            used_bytes=usage.used,
            free_bytes=usage.free,
            free_ratio=free_ratio,
            status=self.classify(free_ratio),
            warning_free_ratio=self.warning_free_ratio,
            critical_free_ratio=self.critical_free_ratio,
        )

    def require_ingest_capacity(self, path: Path) -> CapacitySnapshot:
        snapshot = self.inspect(path)
        if snapshot.status == "critical":
            raise CapacityProtectedError("capacity critical; new ingest is disabled", snapshot)
        return snapshot

    def require_backfill_capacity(self, path: Path, *, requested_days: int) -> CapacitySnapshot:
        snapshot = self.require_ingest_capacity(path)
        if snapshot.status == "warning" and requested_days > 31:
            raise CapacityProtectedError(
                "capacity warning; unattended backfill over 31 days is disabled", snapshot
            )
        return snapshot


class CapacityProtectedError(RuntimeError):
    def __init__(self, message: str, snapshot: CapacitySnapshot):
        super().__init__(message)
        self.snapshot = snapshot
