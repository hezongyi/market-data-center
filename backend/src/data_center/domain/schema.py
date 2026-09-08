from data_center.domain.models import ProviderBar


REQUIRED_FIELDS = tuple(ProviderBar.model_fields)
SCHEMA_VERSION = "provider_bars.v1"


def validate_provider_bars(rows: list[ProviderBar]) -> None:
    if not rows:
        raise ValueError("provider_bars cannot be empty")
    keys = {(row.provider, row.symbol, row.timeframe, row.bar_ts) for row in rows}
    if len(keys) != len(rows):
        raise ValueError("provider_bars contains duplicate primary keys")
    for row in rows:
        if row.high < max(row.open, row.close) or row.low > min(row.open, row.close):
            raise ValueError(f"invalid OHLC at {row.bar_ts.isoformat()}")

