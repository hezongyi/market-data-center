"""Immutable, cached catalog snapshots for bounded query execution."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from data_center.catalog.manifest import PublicationError, validate_manifest_metadata


@dataclass(frozen=True)
class PartReference:
    path: Path
    manifest_path: Path
    run_id: str
    schema_version: str


@dataclass(frozen=True)
class CatalogSnapshot:
    snapshot_id: str
    dataset_id: str
    selector: tuple[tuple[str, str], ...]
    parts: tuple[PartReference, ...]
    schema_versions: tuple[str, ...]
    created_at: float


def selector_hash(selector: dict[str, str]) -> str:
    encoded = json.dumps(selector, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


class Catalog:
    """Resolve selector-specific immutable snapshots from published manifests."""

    def __init__(self, root: Path, *, snapshot_ttl_seconds: float = 3600.0):
        self.root = Path(root)
        self.snapshot_ttl_seconds = snapshot_ttl_seconds
        self._lock = threading.RLock()
        self._index_fingerprint: tuple[tuple[str, int, int], ...] | None = None
        self._index: dict[str, tuple[PartReference, ...]] = {}
        self._snapshots: dict[str, CatalogSnapshot] = {}
        self.refresh_count = 0
        self.cache_hits = 0
        self.cache_misses = 0

    def _fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        paths = sorted((self.root / ".manifests").glob("*.json"))
        return tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths)

    def _refresh(self) -> None:
        fingerprint = self._fingerprint()
        if fingerprint == self._index_fingerprint:
            self.cache_hits += 1
            return
        index: dict[str, list[PartReference]] = {}
        for path_text, _, _ in fingerprint:
            manifest_path = Path(path_text)
            try:
                manifest = json.loads(manifest_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise PublicationError("manifest metadata validation failed") from exc
            files = validate_manifest_metadata(self.root, manifest)
            bucket = index.setdefault(manifest["dataset_id"], [])
            bucket.extend(
                PartReference(path=file_path, manifest_path=manifest_path,
                              run_id=manifest["run_id"], schema_version=manifest["schema_version"])
                for file_path in files
            )
        self._index = {dataset: tuple(parts) for dataset, parts in index.items()}
        self._index_fingerprint = fingerprint
        self.refresh_count += 1
        self.cache_misses += 1

    @staticmethod
    def _matches(part: PartReference, selector: dict[str, str]) -> bool:
        path_parts = set(part.path.parts)
        return all(f"{key}={value}" in path_parts for key, value in selector.items())

    def resolve(self, dataset_id: str, selector: dict[str, str]) -> CatalogSnapshot:
        with self._lock:
            self._refresh()
            parts = tuple(part for part in self._index.get(dataset_id, ()) if self._matches(part, selector))
            identity = {
                "dataset_id": dataset_id,
                "selector": sorted(selector.items()),
                "parts": [(str(part.path.relative_to(self.root)), part.run_id, part.schema_version) for part in parts],
            }
            snapshot_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot is None:
                snapshot = CatalogSnapshot(
                    snapshot_id=snapshot_id,
                    dataset_id=dataset_id,
                    selector=tuple(sorted(selector.items())),
                    parts=parts,
                    schema_versions=tuple(sorted({part.schema_version for part in parts})),
                    created_at=time.time(),
                )
                self._snapshots[snapshot_id] = snapshot
            self._expire_snapshots()
            return snapshot

    def get(self, snapshot_id: str) -> CatalogSnapshot | None:
        with self._lock:
            self._expire_snapshots()
            return self._snapshots.get(snapshot_id)

    def _expire_snapshots(self) -> None:
        cutoff = time.time() - self.snapshot_ttl_seconds
        expired = [key for key, value in self._snapshots.items() if value.created_at < cutoff]
        for key in expired:
            del self._snapshots[key]
