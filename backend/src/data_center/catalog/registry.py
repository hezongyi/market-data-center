DATASETS = {
    "provider_bars": {
        "dataset_id": "provider_bars",
        "schema_version": "v1",
        "description": "标准化 provider 原始行情 bars",
        "partitioning": ["provider", "asset_class", "symbol", "timeframe", "year"],
    },
    "economic_observations": {
        "dataset_id": "economic_observations",
        "schema_version": "v1",
        "description": "标准化宏观经济时间序列",
        "partitioning": ["provider", "series_id"],
        "required_fields": ["series_id", "provider", "observation_date", "value", "ingest_ts"],
    },
}
