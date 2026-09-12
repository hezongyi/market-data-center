from data_center.domain.models import MarketBar, ProviderBar

REQUIRED_FIELDS = tuple(ProviderBar.model_fields)
SCHEMA_VERSION = "provider_bars.v1"
ECONOMIC_SCHEMA_VERSION = "economic_observations.v1"
ECONOMIC_PIT_SCHEMA_VERSION = "economic_observations.v2"
ECONOMIC_REQUIRED_FIELDS = ("series_id", "provider", "observation_date", "release_ts", "asof_ts", "value",
                            "frequency", "units", "seasonal_adjustment", "vintage_start", "vintage_end",
                            "availability_policy", "availability_lag_days", "ingest_ts", "source_hash")
ECONOMIC_PIT_REQUIRED_FIELDS = ECONOMIC_REQUIRED_FIELDS + ("source", "missing_reason")
MARKET_BARS_SCHEMA_VERSION = "market_bars.v1"


def validate_provider_bars(rows: list[ProviderBar]) -> None:
    if not rows:
        raise ValueError("provider_bars cannot be empty")
    keys = {(row.provider, row.symbol, row.timeframe, row.bar_ts) for row in rows}
    if len(keys) != len(rows):
        raise ValueError("provider_bars contains duplicate primary keys")
    timestamps = [row.bar_ts for row in rows]
    if any(timestamp.tzinfo is None or timestamp.utcoffset() is None for timestamp in timestamps):
        raise ValueError("provider_bars timestamps must be timezone-aware")
    if any(timestamp.utcoffset().total_seconds() != 0 for timestamp in timestamps):
        raise ValueError("provider_bars timestamps must be UTC")
    if timestamps != sorted(timestamps):
        raise ValueError("provider_bars timestamps must be sorted ascending")
    for row in rows:
        if row.high < max(row.open, row.close) or row.low > min(row.open, row.close):
            raise ValueError(f"invalid OHLC at {row.bar_ts.isoformat()}")


def validate_market_bars(rows: list[MarketBar]) -> None:
    if not rows:
        raise ValueError("market_bars cannot be empty")
    keys = {(row.provider, row.symbol, row.timeframe, row.bar_ts, row.price_basis,
             row.session_profile, row.recipe_id, row.recipe_version) for row in rows}
    if len(keys) != len(rows):
        raise ValueError("market_bars contains duplicate primary keys")
    if [row.bar_ts for row in rows] != sorted(row.bar_ts for row in rows):
        raise ValueError("market_bars timestamps must be sorted ascending")
    for row in rows:
        if row.bar_ts.tzinfo is None or row.ingest_ts.tzinfo is None:
            raise ValueError("market_bars timestamps must be timezone-aware")
        if row.bar_ts.utcoffset() is None or row.ingest_ts.utcoffset() is None:
            raise ValueError("market_bars timestamps must be timezone-aware")
        if row.bar_ts.utcoffset().total_seconds() != 0 or row.ingest_ts.utcoffset().total_seconds() != 0:
            raise ValueError("market_bars timestamps must be UTC")
        if row.high < max(row.open, row.close) or row.low > min(row.open, row.close):
            raise ValueError(f"invalid OHLC at {row.bar_ts.isoformat()}")
        if not all((row.recipe_id, row.recipe_version, row.input_snapshot_id, row.source_hash)):
            raise ValueError("market_bars lineage is incomplete")


def validate_economic_observations(rows: list[dict], *, schema_version: str = ECONOMIC_SCHEMA_VERSION) -> None:
    if not rows:
        raise ValueError("economic_observations cannot be empty")
    if schema_version not in {ECONOMIC_SCHEMA_VERSION, ECONOMIC_PIT_SCHEMA_VERSION}:
        raise ValueError(f"unsupported economic_observations schema: {schema_version}")
    required = (ECONOMIC_PIT_REQUIRED_FIELDS if schema_version == ECONOMIC_PIT_SCHEMA_VERSION
                else ECONOMIC_REQUIRED_FIELDS)
    missing = [field for field in required if field not in rows[0]]
    if missing:
        raise ValueError(f"economic_observations missing fields: {', '.join(missing)}")
    allowed = {"realtime_vintage", "release_date_known", "release_date_unknown_ingest_asof"}
    for row in rows:
        if row.get("availability_policy") not in allowed:
            raise ValueError("economic_observations has invalid availability_policy")
        policy = row["availability_policy"]
        if policy == "realtime_vintage" and not row.get("vintage_start"):
            raise ValueError("realtime_vintage requires vintage_start")
        if policy == "release_date_known" and not row.get("release_ts"):
            raise ValueError("release_date_known requires release_ts")
        if policy == "release_date_unknown_ingest_asof" and row.get("release_ts") is not None:
            raise ValueError("unknown release policy cannot contain release_ts")
        if schema_version == ECONOMIC_PIT_SCHEMA_VERSION:
            if not row.get("source"):
                raise ValueError("economic_observations.v2 requires source")
            if row.get("value") is None and not row.get("missing_reason"):
                raise ValueError("missing economic values require missing_reason")
