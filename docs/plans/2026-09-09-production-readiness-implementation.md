# Production Readiness Implementation Plan

## P1 Receipt 与状态

- 扩展 run/job ledger schema
- 补 input hash、connector version、quality summary、retry/error 字段
- 固定状态迁移和不可变 receipt 规则

## P2 真实 provider acceptance

- Binance `BTCUSDT 1d`
- yfinance `SPY 1d`
- FRED `PAYEMS`
- 每条链路保存 row count、时间范围、hash、quality 和 receipt

## P3 Worker 可靠性

- retry、timeout、heartbeat
- worker 重启后继续领取 queued job
- dead-letter 查询和 Web UI 展示

## P4 服务可观测性

- live/ready/metrics endpoints
- Web UI 增加 worker、ready 和失败任务状态
- 结构化日志和 request id

## P5 部署验收

- systemd user service/timer 使用固定环境配置
- service restart/recovery 验收
- canonical root smoke 和回滚说明

## P6 下游迁移准备

- `macro-market-lab` 只读 adapter parity test
- 先迁移低风险 consumer
- 经济 PIT schema 兼容后再迁移 economic consumer

