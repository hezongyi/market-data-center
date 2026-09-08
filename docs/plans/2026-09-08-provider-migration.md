# Provider Migration Plan

## Phase 1：MVP 稳定

- fixture `provider_bars` ingest/query/quality/receipt
- API key、worker、systemd、Web UI
- API contract tests and local smoke

## Phase 2：第一个真实 provider

优先接入 Binance 或 yfinance，保持现有 `ProviderBar` schema 不变。每个 provider 只负责 fetch 和 provider-specific normalization；不得把 provider 响应暴露给 API。

## Phase 3：宏观数据

接入 FRED `economic_observations`，随后接入 `economic_events`。release time、vintage 和 availability 语义必须在独立 dataset contract 中冻结。

FRED 已具备 canonical ingest、`economic_observations.v1` schema、realtime vintage 语义、不可变 Parquet part 和最小质量检查。当前 provider-bars ingest 已纳入 durable worker queue，并提供 coverage API。

`macro-market-lab` 的正式 economic consumer 目前依赖 richer PIT contract，包括 `release_ts`、frequency、units、seasonal adjustment 与 series-specific availability policy。它不能直接消费 `economic_observations.v1`；先补齐兼容 dataset contract 和 parity tests，再迁移首个只读 consumer。

## Phase 4：下游切换

在 `macro-market-lab` 增加 `DataCenterClient` adapter，先切换 bars/economic observations 的只读查询，完成新旧结果对比后再切换 ingest、quality 和 maintenance。

## 验收原则

每个 provider 必须有 fixture/mock 测试、schema 校验、质量 finding、receipt 和真实 smoke。迁移期间以 output hash、row count、时间范围和 quality status 做双读对比。
