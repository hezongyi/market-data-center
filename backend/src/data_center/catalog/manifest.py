"""Immutable run publication: all parts are validated before one atomic manifest link."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from data_center.domain.models import ProviderBar
from data_center.domain.schema import (
    validate_economic_observations,
    validate_provider_bars,
)
from data_center.quality.checks import check_economic_observations, check_provider_bars


class PublicationError(ValueError):
    """Safe category for invalid publication inputs; no provider response is included."""


def manifest_path(root: Path, run_id: str) -> Path:
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise PublicationError("invalid run id")
    return Path(root) / ".manifests" / f"{run_id}.json"


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def immutable_json(target: Path, value: dict) -> Path:
    """Never replace an existing inode, including when publishers race."""
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, sort_keys=True, indent=2).encode()
    if target.exists():
        if target.read_bytes() != encoded:
            raise PublicationError("immutable publication already exists with different content") from None
        return target
    lock = target.with_suffix(target.suffix + ".lock")
    try:
        lock.mkdir()
    except FileExistsError:
        # Another publisher owns the short critical section; its result is authoritative.
        if target.exists() and target.read_bytes() == encoded:
            return target
        raise PublicationError("manifest publication is busy") from None
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if target.exists():
            if target.read_bytes() != encoded:
                raise PublicationError("immutable publication already exists with different content")
        else:
            os.rename(temporary, target)
            temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock.rmdir()
    return target


def build_manifest(root: Path, *, run_id: str, dataset_id: str, schema_version: str,
                   paths: list[Path], row_count: int, quality_summary: dict) -> dict:
    parts = [{"path": str(path.resolve().relative_to(root.resolve())),
              "bytes": path.stat().st_size, "sha256": file_hash(path),
              "schema": {key: str(value) for key, value in pl.read_parquet_schema(path).items()}}
             for path in paths]
    return {"run_id": run_id, "dataset_id": dataset_id, "schema_version": schema_version,
            "parts": parts, "row_count": row_count, "quality_summary": quality_summary,
            "generated_at": datetime.now(timezone.utc).isoformat(), "status": "published"}


def validate_manifest(root: Path, manifest: dict) -> list[Path]:
    try:
        dataset = manifest["dataset_id"]
        if dataset not in {"provider_bars", "economic_observations"}:
            raise ValueError("dataset")
        if manifest["schema_version"] != dataset + ".v1" or manifest["status"] != "published":
            raise ValueError("version/status")
        if manifest["quality_summary"]["status"] != "pass":
            raise ValueError("quality")
        files, rows = [], []
        for item in manifest["parts"]:
            relative = Path(item["path"])
            if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != dataset:
                raise ValueError("path")
            path = root / relative
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("path")
            if path.stat().st_size != item["bytes"] or item["bytes"] <= 0 or file_hash(path) != item["sha256"]:
                raise ValueError("integrity")
            if {k: str(v) for k, v in pl.read_parquet_schema(path).items()} != item["schema"]:
                raise ValueError("schema")
            rows.extend(pl.read_parquet(path).to_dicts())
            files.append(path)
        if not files or len(set(files)) != len(files) or len(rows) != manifest["row_count"]:
            raise ValueError("count")
        if dataset == "provider_bars":
            bars = [ProviderBar.model_validate(row) for row in rows]
            validate_provider_bars(bars)
            findings = check_provider_bars(bars)
        else:
            validate_economic_observations(rows)
            findings = check_economic_observations(rows)
        if findings:
            raise ValueError("quality")
        return files
    except Exception as exc:
        raise PublicationError("manifest validation failed") from exc


def write_manifest(root: Path, manifest: dict) -> Path:
    validate_manifest(root, manifest)
    return immutable_json(manifest_path(root, manifest["run_id"]), manifest)


def published_files(root: Path, dataset_id: str) -> list[Path]:
    result: list[Path] = []
    for path in sorted((root / ".manifests").glob("*.json")):
        manifest = json.loads(path.read_text())
        if manifest.get("dataset_id") == dataset_id:
            # Fail closed rather than silently returning an incomplete dataset.
            result.extend(validate_manifest(root, manifest))
    return result
