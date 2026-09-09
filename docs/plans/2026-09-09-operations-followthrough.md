# 运行维护落地与验收

本轮范围对应用户要求的六项后续建议。以下状态必须由实际执行证据更新，代码存在不等于生产验收通过。

| 项目 | 实现与验收要求 | 状态 |
| --- | --- | --- |
| 代码提交与 CI | 提交已有可验证改动；CI 安装依赖、测试、前端构建、隔离服务 smoke | 已完成；统一入口本机 48 passed，ruff、Web UI build、隔离 service acceptance 通过，commit `19ee748`；托管 workflow 已配置但本轮未触发 |
| 定时 provider 验收 | systemd timer、请求间隔、失败告警文件、receipt 保留策略；真实 timer 触发 | 已完成；timer 已启用并触发；最终手动真实验收三 provider pass，receipt `acceptance-receipts/manual-final-2` |
| 进程超时与恢复 | 超时杀死执行进程，重启不重复领取；验证无迟到写盘、心跳持续更新 | 已完成；进程超时、互斥、恢复测试及隔离服务通过 |
| 失败管理 | failed/dead-letter 查询、授权重试、原 receipt 保留、UI 操作 | 已完成；API/UI 浏览器验收桌面和 390px 通过 |
| 下游接入 | macro-market-lab 只读 adapter parity，迁移低风险数据预览；经济 PIT 不冒充兼容 | 已完成；provider bars adapter parity 通过并保留 feature flag/回滚；economic consumer 明确 `not_migrated` |
| 保留、回补、日志指标 | receipt 归档保留、canonical 保留审计、显式日期回补、结构化 request/run 日志及指标 | 已完成；回补真实 Binance 通过，审计只读，request/run 日志与 metrics 可读 |

仓库已配置 `origin`，`.github/workflows/ci.yml` 与 `scripts/ci.sh` 使用同一入口；本轮未触发托管 workflow，因此不把本机结果当作托管 CI 证据。

保留策略只自动处理定时验收 receipt 与临时运行产物。canonical 历史数据与原始 run receipt 不自动删除；数据回补通过正式入队接口产生新 run 和新 part。

## 正式 spec 收口证据

- `provider_bars` 与 `economic_observations` writer 均经过 catalog path resolver，并拒绝复用已有 part ID；查询层使用 DuckDB 内存连接读取 Parquet，不创建可变查询库。
- 成功 receipt 统一包含 `quality_summary`、`attempt_count`、`retry_count`；质量失败记录 `failure_stage=quality` 和结构化 findings，执行失败记录 `failure_stage=execute`。
- 仓库声明的 systemd acceptance unit（无临时 drop-in）于 `2026-09-09 09:53 UTC` 实际执行成功，Binance、yfinance、FRED 均 `pass`；receipt 为 `acceptance-receipts/scheduled/receipt-9f326e90938c42c5aaa8556224a0b6e6.json`。
- 最新本机 CI 为 48 tests passed、ruff、Web UI build 和 service acceptance passed；systemd 重启后 services/timers 保持 active+enabled，真实 Binance、yfinance、FRED receipt 与 manifest 保存在 `/home/quant/market_lake/evidence/data-center/`。托管 CI 与 economic consumer 全量切换仍未宣称完成。
