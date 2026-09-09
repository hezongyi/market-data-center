# Production Readiness Design

日期：2026-09-09
状态：approved for implementation

## 目标

将当前可运行的 MVP 提升为可承载真实 provider 任务的本地生产服务，重点保证运行可追溯、失败可恢复、服务可观测和 systemd 可验收。

## 范围

- Binance、yfinance、FRED 的真实 acceptance
- 统一 run receipt：`run_id`、`job_id`、`dataset_id`、`schema_version`、provider、connector version、input/output hash、row count、time range、quality summary、retry/error 信息
- 状态：`queued`、`running`、`pass`、`failed`、`dead_letter`
- retry、timeout、worker heartbeat 和 durable queue
- `/api/v1/health/live`、`/api/v1/health/ready`、`/api/v1/metrics`
- API/worker systemd service 与 timer 验收
- canonical root、ledger 和密钥的配置边界

## 非目标

本阶段不实现多用户权限、Postgres/Redis、Kubernetes、自动数据修复、完整 maintenance scheduler 或所有 `macro-market-lab` consumer 迁移。

## 不变量

1. API 和 worker 使用同一个 ledger 与 canonical root。
2. 数据写入只通过 dataset writer 和 catalog resolver，历史 part 不覆盖。
3. provider 原始响应不通过 API 暴露。
4. 失败任务必须保留错误类型和可重试判断；超过重试上限进入 `dead_letter`。
5. readiness 只有在 ledger 可写、canonical root 可访问、worker heartbeat 未过期时才为 ready。
6. 所有真实 acceptance 必须保存 receipt 和验证命令结果。

## 验收

- 三个真实 provider 各有成功或明确受控失败的 acceptance receipt。
- API 入队后 worker 能完成任务并更新同一 `run_id`。
- worker 重启不会丢失 queued job。
- 错误任务按 retry policy 结束为 `failed` 或 `dead_letter`。
- live/ready/metrics 返回稳定 schema。
- systemd service/timer 在重启后保持 enabled/active，并通过 smoke。

