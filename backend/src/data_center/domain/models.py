from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProviderBar(BaseModel):
    symbol: str
    asset_class: str
    provider: str
    timeframe: str
    bar_ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    currency: str = "USD"
    price_type: str = "raw"
    ingest_ts: datetime
    source_hash: str


class MarketBar(BaseModel):
    symbol: str
    asset_class: str
    provider: str
    timeframe: str
    source_timeframe: str
    bar_ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    currency: str = "USD"
    price_basis: str
    session_profile: str
    recipe_id: str
    recipe_version: str
    input_snapshot_id: str
    ingest_ts: datetime
    source_hash: str


class IngestJob(BaseModel):
    job_id: str
    dataset_id: str = "provider_bars"
    provider: str = "fixture"
    symbol: str
    asset_class: str = "crypto"
    timeframe: str = "1d"
    start: datetime
    end: datetime
    run_scope: Literal["production", "acceptance", "migration", "maintenance"] = Field(default="production")
    run_kind: Literal["ingest", "derive", "backfill", "gap_repair", "quality", "parity"] = Field(default="ingest")


class DeriveJob(BaseModel):
    job_id: str
    dataset_id: Literal["market_bars"] = "market_bars"
    provider: str
    symbol: str
    recipe_id: str
    recipe_version: str
    start: datetime
    end: datetime
    input_snapshot_id: str | None = None
    run_scope: Literal["production", "acceptance", "migration", "maintenance"] = "production"
    run_kind: Literal["derive"] = "derive"


class DatasetDefinition(BaseModel):
    """Governed control-plane definition for a dataset.

    The registry serializes this model for API consumers.  Keeping the definition
    as data lets storage, query and publication modules resolve new datasets
    without adding provider-specific branches.
    """

    dataset_id: str
    schema_version: str
    kind: Literal["raw", "derived"]
    description: str = ""
    primary_key: tuple[str, ...] = ()
    partitioning: tuple[str, ...] = ()
    selector_fields: tuple[str, ...] = ()
    required_lineage: tuple[str, ...] = ()
    quality_profile: str = "default"
    query_modes: tuple[str, ...] = ("current",)
    retention_policy: dict[str, Any] = Field(default_factory=dict)
    materialization_policy: Literal["persisted", "ephemeral"] = "persisted"
    publication_policy: Literal["canonical", "research_only"] = "canonical"

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "DatasetDefinition":
        return cls.model_validate(value)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
