DATASETS = {
    "provider_bars": {
        "dataset_id": "provider_bars",
        "schema_version": "v1",
        "description": "标准化 provider 原始行情 bars",
        "partitioning": ["provider", "asset_class", "symbol", "timeframe", "year"],
    }
}

