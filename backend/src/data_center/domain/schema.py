from data_center.domain.models import ProviderBar

REQUIRED_FIELDS = tuple(ProviderBar.model_fields)
SCHEMA_VERSION = "provider_bars.v1"
ECONOMIC_REQUIRED_FIELDS = ("series_id", "provider", "observation_date", "release_ts", "asof_ts", "value",
                            "frequency", "units", "seasonal_adjustment", "vintage_start", "vintage_end",
                            "availability_policy", "availability_lag_days", "ingest_ts", "source_hash")


def validate_provider_bars(rows: list[ProviderBar]) -> None:
    if not rows:
        raise ValueError("provider_bars cannot be empty")
    keys = {(row.provider, row.symbol, row.timeframe, row.bar_ts) for row in rows}
    if len(keys) != len(rows):
        raise ValueError("provider_bars contains duplicate primary keys")
    for row in rows:
        if row.high < max(row.open, row.close) or row.low > min(row.open, row.close):
            raise ValueError(f"invalid OHLC at {row.bar_ts.isoformat()}")


def validate_economic_observations(rows: list[dict]) -> None:
    if not rows:
        raise ValueError("economic_observations cannot be empty")
    missing = [field for field in ECONOMIC_REQUIRED_FIELDS if field not in rows[0]]
    if missing:
        raise ValueError(f"economic_observations missing fields: {', '.join(missing)}")
    allowed = {"realtime_vintage", "release_date_known", "release_date_unknown_ingest_asof"}
    for row in rows:
        if row.get("availability_policy") not in allowed:
            raise ValueError("economic_observations has invalid availability_policy")
