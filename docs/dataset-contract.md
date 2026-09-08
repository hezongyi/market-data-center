# Dataset Contract

## provider_bars v1

标准化 provider 原始行情，主键为 `provider + symbol + timeframe + bar_ts`。

必需字段：`symbol`、`asset_class`、`provider`、`timeframe`、`bar_ts`、`open`、`high`、`low`、`close`、`volume`、`currency`、`price_type`、`ingest_ts`、`source_hash`。

时间字段统一使用 UTC ISO-8601；价格字段为浮点数；缺失成交量允许为 null，但 OHLC 不允许为空。schema 通过 `schema_version` 标识，禁止静默修改字段语义。

