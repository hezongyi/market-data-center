"""Control-plane dataset registry."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from data_center.domain.models import DatasetDefinition

_DEFINITIONS: dict[str, DatasetDefinition] = {
    "provider_bars": DatasetDefinition(
        dataset_id="provider_bars", schema_version="provider_bars.v1", kind="raw",
        description="标准化 provider 原始行情 bars",
        primary_key=("provider", "symbol", "timeframe", "bar_ts"),
        partitioning=("provider", "asset_class", "symbol", "timeframe", "year"),
        selector_fields=("provider", "symbol", "timeframe"),
        required_lineage=("source_hash", "ingest_ts"),
        quality_profile="provider_bars", query_modes=("current",),
        retention_policy={"mode": "immutable"}, materialization_policy="persisted",
    ),
    "economic_observations": DatasetDefinition(
        dataset_id="economic_observations", schema_version="v2", kind="raw",
        description="标准化宏观经济时间序列",
        primary_key=("provider", "series_id", "observation_date", "vintage_start"),
        partitioning=("provider", "series_id"),
        selector_fields=("provider", "series_id"),
        required_lineage=("source_hash", "ingest_ts", "asof_ts"),
        quality_profile="economic_observations", query_modes=("current", "pit"),
        retention_policy={"mode": "immutable"}, materialization_policy="persisted",
    ),
    "market_bars": DatasetDefinition(
        dataset_id="market_bars", schema_version="market_bars.v1", kind="derived",
        description="按版本化 recipe 生成的 canonical market bars",
        primary_key=("provider", "symbol", "timeframe", "bar_ts", "price_basis",
                     "session_profile", "recipe_id", "recipe_version"),
        partitioning=("provider", "asset_class", "symbol", "timeframe", "price_basis", "year"),
        selector_fields=("provider", "symbol", "timeframe", "price_basis"),
        required_lineage=("recipe_id", "recipe_version", "input_snapshot_id", "source_hash"),
        quality_profile="market_bars", query_modes=("current",),
        retention_policy={"mode": "immutable"}, materialization_policy="persisted",
    ),
}


def register_dataset(definition: DatasetDefinition | dict[str, Any], *, replace: bool = False) -> DatasetDefinition:
    """Register a dataset definition, rejecting accidental identity changes."""
    resolved = definition if isinstance(definition, DatasetDefinition) else DatasetDefinition.model_validate(definition)
    if resolved.dataset_id in _DEFINITIONS and not replace:
        existing = _DEFINITIONS[resolved.dataset_id]
        if existing != resolved:
            raise ValueError(f"dataset already registered: {resolved.dataset_id}")
        return existing
    _DEFINITIONS[resolved.dataset_id] = resolved
    refresh_legacy_view()
    return resolved


def get_dataset_definition(dataset_id: str) -> DatasetDefinition:
    try:
        return _DEFINITIONS[dataset_id]
    except KeyError as exc:
        raise ValueError(f"unsupported dataset: {dataset_id}") from exc


def iter_dataset_definitions() -> Iterable[DatasetDefinition]:
    return tuple(_DEFINITIONS.values())


def dataset_definitions() -> dict[str, DatasetDefinition]:
    return dict(_DEFINITIONS)


def refresh_legacy_view() -> None:
    DATASETS.clear()
    DATASETS.update({dataset_id: definition.as_dict() for dataset_id, definition in _DEFINITIONS.items()})


# Backwards-compatible API shape used by the HTTP endpoint and existing clients.
DATASETS: dict[str, dict[str, Any]] = {}
refresh_legacy_view()
