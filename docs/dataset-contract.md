# Dataset Contract

所有 dataset 由 control-plane `DatasetDefinition` 登记。定义至少包含 schema version、
`kind=raw|derived`、主键、分区、selector、required lineage、quality profile、query mode、
retention 和 materialization policy。catalog、manifest 和 query 通过 registry 解析 dataset，
新增 dataset 不得分别维护硬编码白名单。

## provider_bars v1

标准化 provider 原始行情，主键为 `provider + symbol + timeframe + bar_ts`。

必需字段：`symbol`、`asset_class`、`provider`、`timeframe`、`bar_ts`、`open`、`high`、`low`、`close`、`volume`、`currency`、`price_type`、`ingest_ts`、`source_hash`。

时间字段统一使用 UTC ISO-8601；价格字段为浮点数；缺失成交量允许为 null，但 OHLC 不允许为空。schema 通过 `schema_version` 标识，禁止静默修改字段语义。

## economic_observations v1

标准化经济时间序列，逻辑主键为 `provider + series_id + observation_date + vintage_start`。必需字段为 `series_id`、`provider`、`observation_date`、`release_ts`、`asof_ts`、`value`、`frequency`、`units`、`seasonal_adjustment`、`vintage_start`、`vintage_end`、`availability_policy`、`availability_lag_days`、`ingest_ts`、`source_hash`。

`value` 可为 null，表示 provider 明确报告缺失值。FRED 通过 `vintage_start` / `vintage_end` 保存 `realtime_start` / `realtime_end`；不虚构未由 source 提供的 `release_ts`。`availability_policy` 只允许 `realtime_vintage`、`release_date_known` 和 `release_date_unknown_ingest_asof`。读取当前态时，每个 `observation_date` 选择最新 `vintage_start`。

## economic_observations v2（PIT）

v2 保留 v1 字段，并增加 `source` 和 `missing_reason`。`source` 标识规范化来源；当 `value` 为 null 时，`missing_reason` 必须说明 provider 缺失语义。`release_ts` 只有 provider 明确提供时才填写，`asof_ts` 表示数据中心观察到该版本的时间。`realtime_vintage` 通过 `vintage_start` 表示可见日期，`release_date_known` 通过 `release_ts` 表示可见时间，`release_date_unknown_ingest_asof` 永远不能被 PIT 查询当作已知发布版本。

current-state 查询对每个 `observation_date` 选择最新可用 vintage；PIT 查询必须给出 `asof_ts`，只返回在该时点已知且有明确 release/vintage 可见语义的版本。API 使用 `mode=current|pit` 明确区分两种读取。

## market_bars v1

`market_bars` 是独立的 derived dataset，不回写 `provider_bars`。主键为
`provider + symbol + timeframe + bar_ts + price_basis + session_profile + recipe_id + recipe_version`。

每行必须记录 `source_timeframe`、`price_basis`、`session_profile`、`recipe_id/version`、
`input_snapshot_id`、`ingest_ts` 和 `source_hash`。manifest 还必须记录 input dataset/snapshot、
输入 source hash 的有界摘要（count、digest、首尾 hash），以及 calendar/session、aggregation version
和所有控制面配置 digest；完整集合可从 immutable input parts 重建，receipt 不保存无界 hash 数组。
正式查询必须指定 recipe identity 和 price basis，避免把不同派生语义混成一个 current state。
