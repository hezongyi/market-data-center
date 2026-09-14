# Market Data Center 当前状态

更新时间：2026-09-14。本文描述当前生产事实；历史 receipt、旧 deployment 和阶段性计划保留原文，不代表当前状态。

## 生产运行

| 项目 | 当前事实 | 证据 |
| --- | --- | --- |
| 当前 deployment | `3e2362ab0b31-6005b252`（`software_version=0.5.0`，`source_commit=3e2362a`，`tag=v0.5.0`） | `operations/deployment_activate/2026-09-13T234558…json`（`tag` 由 `operations/deployment_stage/2026-09-13T234519…json` 记录） |
| API/worker | systemd active；readiness=ready | `/api/v1/health/ready` |
| 容量/队列 | `capacity_status=ok`、queue=0 | `/api/v1/metrics` |
| 生产配置来源 | API/worker 只从机器级 `$HOME/.config/market-data-center/env`（0600）读取环境；unit 无 drop-in，生产进程不再引用任何仓库 checkout | `systemctl --user show -p DropInPaths`（两服务均为空）+ 进程环境键名 |
| Dukascopy raw | `provider_bars`、1m、BID-only | `2026-09-11-dukascopy-1m-bid-rollout.md` |
| Dukascopy derived | 5m/15m/30m/1h/4h/1d recipes；完整历史受 coverage 约束 | `dukascopy_derived_multiperiod_acceptance_v3` |
| Dukascopy maintenance | macro-market-lab systemd service 调用 Data Center runner | `macro_market_lab_maintenance_ownership_cutover` |

## 版本与发布基线

| 项目 | 当前事实 | 证据 |
| --- | --- | --- |
| 源码版本 | `0.5.1`；`backend/pyproject.toml`、`webui/package.json` 与 `data_center.__version__` 三者一致（生产进程仍运行 `0.5.0`，见下） | `v0.5.1` release contract test |
| 最新发布标签 | `v0.5.1` → `edc6c1c442e4960fdd82d08fd6db4da279d0e275`（annotated tag object `859469aa…`；按仓库约定发布后不再移动） | [GitHub release](https://github.com/hezongyi/market-data-center/releases/tag/v0.5.1) + `release-receipt.json`（`result=pass`） |
| 生产 deployment source commit | `3e2362ab0b31a194639f3e4801322bb6d14f1ee6`（= `v0.5.0`）；`v0.5.1` 已打标签但**未激活**，生产 deployment 不随新标签自动前进 | active deployment manifest + `git log --oneline v0.5.0..origin/main` |
| 基线规则 | 生产 deployment 只能由 commit-scoped `verify` 成功的 protected-main commit 创建；release 标签只打在该 commit 上且不可移动 | `docs/release-checklist.md` |

生产 deployment 现与 `v0.5.0` 发布基线一致（WebUI v0.5 可用性、访问控制和版本收口）。上一版本 `v0.4.1` 的 stage/activate、回滚路径和 receipt 均保留，可作为回滚目标；本次 `v0.5.0` activation 的 canonical 与 ledger 哈希均未变化。更早版本的 stage/activate、注入候选 readiness 失败后的自动恢复、回滚和 monitor soak receipt 也继续保留在 data-center evidence root。升级只允许走 immutable activation 流程，不得手工改动 systemd unit 或依赖。

2026-09-14 发布 `v0.5.1`（**仅打标签与发布 GitHub Release，不推生产**）：把 `v0.5.0` 之后合入的修复收口为补丁版——WebUI 跨工作区移交（#82/#83）、探索页提交语义（#84）、覆盖度"未计算"的原因（#85）、容量测量来源（#86）、验收 receipt 可判读（#94）、告警闸门收窄（#95）、生产配置来源守卫（#92/#93）。发布准备 PR #102 以 squash 合入为 protected-main 提交 `edc6c1c442e4960fdd82d08fd6db4da279d0e275`，其 commit-scoped `verify`（run `34804428044`）成功；本地统一门禁在与之树等价的 `d8aed91` 上 `result=pass`（`software_version=0.5.1`，276 passed / 4 skipped，浏览器验收 57 checks 双视口）。`Release` run `34804615895` 发布了 `v0.5.1` 与 `release-receipt.json`。

**本次没有执行 `deployment_stage`/`deployment_activate`**：生产继续服务 `v0.5.0`（`3e2362ab0b31-6005b252`），readiness 仍为 `ready`，canonical 与 ledger 未受影响；`v0.5.1` 的激活需要另行审批与维护窗口，届时按 `docs/release-checklist.md` 的 immutable activation 清单执行。发布证据见 `docs/release-checklist.md` 的 "v0.5.1 protected-main evidence" 与 `docs/releases/v0.5.1.md`（后者同时记录真实 provider 验收仍为红的观察项）。

2026-09-14 收口生产配置来源：host-local drop-in `provider-env.conf` 曾让 API/worker 额外读取 `market-data-center-latest/.env.local`（仓库 checkout）。它与机器级 env 的三个共有键（`DATACENTER_API_KEY`、`DATACENTER_PROXY_URL`、`FRED_API_KEY`）取值一致，另含一个生产不使用的 `GITHUB_TOKEN`，因此两个 drop-in 已移除，机器级 env 成为唯一配置来源；provider 通路不受影响（`DATACENTER_PROXY_URL` 仍在进程环境中）。移除后 `DropInPaths` 为空、进程环境不再含 `GITHUB_TOKEN`，`deployment_id`/`software_version`/`source_commit` 与 `v0.5.0` 基线保持一致（本次不涉及 release）。回滚副本保留在 `$HOME/market-data-center/config-history/2026-09-14/`，receipt 见 evidence root `operations/production_env_source_consolidation/`。约束不变：仍不得手工改动 immutable release 的 unit 或依赖，生产行为变更必须走 approval。

monitor timer 配置为 `OnUnitInactiveSec=60s`，但实测节奏为约 120s（systemd 默认 `AccuracySec=1min` 的合并效应），即告警分辨率实际减半；这是配置事实，不是故障。

## 生产任务与统一调度（开发完成，**未进入生产**）

本节记录开发状态，不是生产事实：调度器尚未安装、尚未激活，生产仍由旧入口服务。

| 项目 | 当前事实 | 证据 |
| --- | --- | --- |
| 调度器单元 | **未安装**：主机 `systemctl --user list-unit-files` 中没有 `market-data-center-scheduler.*` | `systemctl --user list-unit-files --type=service --type=timer`（输出中仅有 api/worker/monitor/smoke/provider-acceptance/1m-maintenance 与 marketlab-market-bars-maintenance） |
| 生产旧入口（现状） | raw：`market-data-center-1m-maintenance.{service,timer}`（`OnUnitInactiveSec=15min`，`data_center.maintenance_runner`）；derived：`marketlab-market-bars-maintenance.{service,timer}`（`data_center.derived_maintenance_runner`，仓库内**未声明**，仅主机安装） | 同上命令 + `deploy/systemd/market-data-center-1m-maintenance.{service,timer}` |
| 交付状态 | 五层 stacked PR #110–#114（issue #109），栈顶 `1a63ac9`；本地统一门禁 `result=pass`（438 passed / 5 skipped，浏览器验收 65 checks 双视口） | 每层 PR 的 hosted `verify` 与本地 `acceptance-receipts/ci/all.json` |
| ledger schema | 生产库**仍是旧版本**；开发分支把迁移链推进到 `SCHEMA_VERSION=5`（新增 `provider_backoff`），只有该 release 被激活时才会在生产库上执行，届时与 API/worker 一起重启 | `backend/src/data_center/runs/ledger.py`、`backend/tests/test_scheduler_service_drill.py`（上一版数据库就地升级演练） |
| 接管准备 | 工具**只准备不执行**：`data_center.takeover` 可盘点仓库声明与主机实际安装的单元、把旧入口导入为 `paused` 计划（默认 dry-run）并输出对照/校验 receipt；本机盘点已识别出上表两个旧入口（其中 marketlab 一个为 host-only） | `backend/src/data_center/takeover.py`、`backend/tests/test_takeover_preparation.py`、`docs/operations-runbook.md` 的 "Legacy timer takeover" 一节 |
| 未执行（需批准） | `retention-audit` 独立单元的安装、调度器单元的安装与影子期观察、旧入口停用、canary 启用、回滚演练 | 计划 S5.2 第 3–8 步；均需维护者批准与生产窗口 |
| 发布边界 | `retention-audit` 解耦（独立单元 + `retention_audit` receipt action）必须作为**独立小发布**进入生产，不并入调度器接管批次 | `docs/operations-runbook.md` 的 "Retention audit is its own release" |

接管顺序（批准后按 `docs/operations-runbook.md` 执行）：部署含调度器的 release → 影子模式观察并与旧 timer 的窗口对照 → 导入旧入口为 `paused` 计划 → 停用旧单元 → canary 启用 1–2 条计划 → 观察后扩大范围；任一步异常按反向顺序回滚，两个方向都留 receipt。

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
