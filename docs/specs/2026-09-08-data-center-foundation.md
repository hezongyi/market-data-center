# Data Center Foundation Specification

日期：2026-09-08
状态：implemented / accepted（MVP）

## 目标

建立独立的数据中心服务，为多个 repo 提供受治理的数据资产存储、接入、质量检查、运行审计和查询能力，并通过 REST API 与 Web UI 暴露统一操作面。

## 边界

数据中心负责 dataset contract、provider connector、canonical storage、ingest、quality、lineage、run ledger 和查询 API。研究、策略、报告、图表、交易记录和 Obsidian 发布继续由 `macro-market-lab` 等上层 repo 负责。

## 技术决策

- Backend：Python、FastAPI、Pydantic
- Web UI：React、TypeScript、Vite
- Canonical storage：Parquet
- Transform/quality：Polars
- Read-only query：DuckDB
- Run ledger：SQLite
- API prefix：`/api/v1`
- Initial deployment：独立 API service + worker + local systemd

## MVP

首个闭环只实现 `provider_bars`、fixture connector、schema validation、Parquet write、quality check、immutable receipt、查询 API 和基础管理页面。真实 provider 在 fixture 闭环稳定后逐个接入。

## 迁移策略

新 repo 先独立实现 core 和 API；`macro-market-lab` 通过 `DataCenterClient` 逐步切换只读查询，再迁移 ingest、quality 和 maintenance。不得让 Web UI 或其他 repo 直接依赖 canonical 文件路径、SQLite 表结构或 provider 原始响应。
