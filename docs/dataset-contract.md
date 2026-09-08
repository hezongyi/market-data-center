# Dataset Contract

## provider_bars v1

标准化 provider 原始行情，主键为 `provider + symbol + timeframe + bar_ts`。

必需字段：`symbol`、`asset_class`、`provider`、`timeframe`、`bar_ts`、`open`、`high`、`low`、`close`、`volume`、`currency`、`price_type`、`ingest_ts`、`source_hash`。

时间字段统一使用 UTC ISO-8601；价格字段为浮点数；缺失成交量允许为 null，但 OHLC 不允许为空。schema 通过 `schema_version` 标识，禁止静默修改字段语义。

## economic_observations v1

标准化经济时间序列，逻辑主键为 `provider + series_id + observation_date + vintage_start`。必需字段为 `series_id`、`provider`、`observation_date`、`value`、`ingest_ts`、`asof_ts`、`availability_policy`、`source_hash`。

`value` 可为 null，表示 provider 明确报告缺失值。FRED 通过 `vintage_start` / `vintage_end` 保存 `realtime_start` / `realtime_end`；不虚构未由 source 提供的 `release_ts`。读取当前态时，每个 `observation_date` 选择最新 `vintage_start`。
