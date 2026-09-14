# Market Data Center 当前状态

更新时间：2026-09-14。本文描述当前生产事实；历史 receipt、旧 deployment 和阶段性计划保留原文，不代表当前状态。

## 生产运行

| 项目 | 当前事实 | 证据 |
| --- | --- | --- |
| 当前 deployment | `3e2362ab0b31-6005b252`（`software_version=0.5.0`，`source_commit=3e2362a`，`tag=v0.5.0`） | `operations/deployment_activate/2026-09-13T234558…json`（`tag` 由 `operations/deployment_stage/2026-09-13T234519…json` 记录） |
| API/worker | systemd active；readiness=ready | `/api/v1/health/ready` |
| 容量/队列 | `capacity_status=ok`、queue=0 | `/api/v1/metrics` |
| Dukascopy raw | `provider_bars`、1m、BID-only | `2026-09-11-dukascopy-1m-bid-rollout.md` |
| Dukascopy derived | 5m/15m/30m/1h/4h/1d recipes；完整历史受 coverage 约束 | `dukascopy_derived_multiperiod_acceptance_v3` |
| Dukascopy maintenance | macro-market-lab systemd service 调用 Data Center runner | `macro_market_lab_maintenance_ownership_cutover` |

## 版本与发布基线

| 项目 | 当前事实 | 证据 |
| --- | --- | --- |
| 源码版本 | `0.5.0`；`backend/pyproject.toml`、`webui/package.json` 与 `data_center.__version__` 三者一致 | `v0.5.0` release contract test |
| 最新发布标签 | `v0.5.0` → `3e2362ab0b31a194639f3e4801322bb6d14f1ee6`（annotated；按仓库约定发布后不再移动） | [GitHub release](https://github.com/hezongyi/market-data-center/releases/tag/v0.5.0) + `release-receipt.json` |
| 生产 deployment source commit | `3e2362ab0b31a194639f3e4801322bb6d14f1ee6`（= `v0.5.0`），是 protected `main` 的当前提交 | active deployment manifest |
| 基线规则 | 生产 deployment 只能由 commit-scoped `verify` 成功的 protected-main commit 创建；release 标签只打在该 commit 上且不可移动 | `docs/release-checklist.md` |

生产 deployment 现与 `v0.5.0` 发布基线一致（WebUI v0.5 可用性、访问控制和版本收口）。上一版本 `v0.4.1` 的 stage/activate、回滚路径和 receipt 均保留，可作为回滚目标；本次 `v0.5.0` activation 的 canonical 与 ledger 哈希均未变化。更早版本的 stage/activate、注入候选 readiness 失败后的自动恢复、回滚和 monitor soak receipt 也继续保留在 data-center evidence root。升级只允许走 immutable activation 流程，不得手工改动 systemd unit 或依赖。

monitor timer 配置为 `OnUnitInactiveSec=60s`，但实测节奏为约 120s（systemd 默认 `AccuracySec=1min` 的合并效应），即告警分辨率实际减半；这是配置事实，不是故障。

## Consumer / ownership 矩阵

| Consumer/数据域 | 当前路径 | 状态 | 回滚/边界 |
| --- | --- | --- | --- |
| macro-market-lab bar preview/query | Data Center HTTP adapter | 已默认切换 | `MACRO_MARKET_USE_DATA_CENTER_BARS=0` |
| macro-market-lab Dukascopy raw/derived maintenance | Data Center runner | 已切换 | `MARKETLAB_MARKET_BARS_BACKEND=legacy` |
| yfinance DXY/SPY/QQQ/TLT macro-daily | macro-market-lab legacy workflow | 未迁移 | 独立数据域 |
| economic PIT/current consumers | 现有 flag/legacy 路径 | 未完成全量切换 | 必须先完成 PIT parity |
| ASK/MID | 未采集 | 第一阶段非目标 | 需独立 identity/API/spec |

## WebUI 数据维护工作台（v0.5，deployed）

| 项目 | 当前事实 | 证据 |
| --- | --- | --- |
| 分支 | `main`（PR #89 WebUI v0.5、PR #90 v0.5.0 发布准备） | git worktree |
| 维护任务 | `POST /maintenance/plans` 无副作用预览 + `POST /maintenance/tasks` 统一 queued envelope；`/derive/runs`、`/economic/ingest`、`/quality/checks` 复用同一 contract | `backend/tests/test_maintenance_contract.py` |
| 只读校验运行 | `quality`/`parity` run 只记录 findings，不发布 canonical part、不产生 manifest | `test_quality_run_executes_as_a_verification_and_records_findings` |
| Runs 读模型 | kind/scope/时间筛选 + opaque cursor 分页；`/runs/{id}/detail` 投影 stage、window、retry chain、degraded 原因，不改写 terminal receipt | `test_run_list_filters_and_cursor_pagination`、`test_run_detail_projects_stage_windows_and_retry_chain` |
| findings 治理 | 稳定 `finding_id`、occurrence 计数、`open/acknowledged/resolved` 处理状态与运行结果分离 | `test_findings_support_structured_filters_and_state_transitions` |
| 写保护 | capacity critical 与 warning 下 >31 天 backfill 返回 507 并进入写审计；鉴权失败 401 | `test_capacity_critical_protects_writes_and_is_audited` |
| 浏览器验收 | 1440px 与 390px 覆盖 provider ingest、derive、parity、quality（degraded）、economic ingest（本地 provider fixture）、被拒写入与容量保护写入 | `acceptance-receipts/browser/receipt.json` |
| 部署状态 | 已发布并激活 `v0.5.0`（`3e2362ab0b31-6005b252`）；`/operations/receipts` 按真实记录的动作返回，v0.4.1 的回滚路径和历史 receipt 仍保留 | `operations/deployment_stage/2026-09-13T234519…json`、`operations/deployment_activate/2026-09-13T234558…json`、生产只读走查命令与 API/UI 输出 |

2026-09-13T23:56Z 完成 v0.5.0 生产只读走查。GET `/api/v1/health`、`/health/live`、`/health/ready`、`/metrics`、`/capabilities`、`/datasets`、`/runs`（含首条 run 的 detail/manifest）、`/quality/findings`、`/maintenance/tasks`、`/operations/queue`、`/operations/capacity-history`、`/operations/worker`、`/operations/receipts` 和 `/openapi.json` 均按预期返回；未调用任何 POST/PATCH/DELETE 写接口。走查时 readiness 为 `ready`，capacity 为 `ok`，queue 为 0，API/worker 为 active，所有身份字段均为 `v0.5.0` / `3e2362a` / `3e2362ab0b31-6005b252`。1440px 浏览器只读验收覆盖总览、数据目录、维护任务、运行记录、质量、数据浏览和运维页面；核心导航为中文，默认 UTC+8，切换 dual 后同时显示 UTC+8 与 UTC，容量文案可见，页面均有内容。深层动态文案的完整中文化仍按独立 spec 延后。

## 状态语义

`implemented` 表示代码/测试存在；`deployed` 表示进入 immutable release；`accepted` 表示有真实 receipt；`default cutover` 表示默认走新路径且有回滚；`not migrated` 表示明确仍走旧路径。

## 未完成事项

1. yfinance macro-daily 数据域迁移。
2. economic PIT/current consumer 全量切换。
3. Dukascopy 历史 provider gap 不补造；高周期完整历史覆盖不作为已完成条件。
4. 1m maintenance 的 `failed` 语义已收敛：provider 无数据（`ProviderGapError`）与 provider 覆盖不完整（`coverage_not_ready`）均记为 `degraded`，cooldown 抑制同样为 `degraded`，结构性质量失败仍为 `failed`。生产实测：2026-09-13 03:11:52 的运行 `result=pass`、`failed_target_count=0`、`degraded_target_count=1`（BTCUSD 17 个 degraded 窗口），systemd 单元 `Result=success`；数据始终 `quality_status=pass`，未被伪造或丢失。
