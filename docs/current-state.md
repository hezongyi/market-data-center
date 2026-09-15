# Market Data Center 当前状态

开发排期更新（2026-09-15）：当前目标改为[EURUSD 独立闭环](specs/2026-09-15-eurusd-first-product-baseline.md)，旧迁移接管与扩面暂停排期。本次基线整理未执行生产变更。

生产观察更新：2026-09-15 04:23–04:25 UTC 的只读查询观察到 v0.6.2 / `95ea63daf79e-17fd1d8f`，账本派发开启、进程仍不派发，EURUSD raw-only。记录于[调查 §2.1](research/2026-09-15-eurusd-first-delivery-and-workflow-proposal.md#21-现场只读快照与事实边界)。操作前重新读取现场，不能从文档判断此后实时状态。

以下表格保留更新时间 2026-09-15 02:06 UTC 的历史生产快照，已被上方有时间的观察局部取代，不代表当前状态。

## 生产运行

| 项目 | 当前事实 | 证据 |
| --- | --- | --- |
| 当前 deployment | `aca73a045275-c6470772`（`software_version=0.6.1`，`source_commit=aca73a0`，`tag=v0.6.1`） | `operations/deployment_activate/2026-09-15T012654.491229+0000-148a0c02052a4403b4b44da872c1d558.json`（pass，canonical/ledger 哈希未变） |
| API/worker/scheduler | systemd active；readiness=ready；组件身份一致 | `/api/v1/health/ready` + `systemctl --user show` |
| 容量/队列 | `capacity_status=ok`（free ratio ≈11.68%，生产阈值 5%/2%）；queued=0、running=0 | `/api/v1/metrics`，2026-09-15 02:06 UTC |
| 生产配置来源 | API/worker 只从机器级 `$HOME/.config/market-data-center/env`（0600）读取环境；unit 无 drop-in，生产进程不再引用任何仓库 checkout | `systemctl --user show -p DropInPaths`（两服务均为空）+ 进程环境键名 |
| Dukascopy raw | `provider_bars`、1m、BID-only | `2026-09-11-dukascopy-1m-bid-rollout.md` |
| Dukascopy derived | 5m/15m/30m/1h/4h/1d recipes；完整历史受 coverage 约束 | `dukascopy_derived_multiperiod_acceptance_v3` |
| 行情维护接管 | 9 条计划已导入，EURUSD raw-only enabled、其余 8 条 paused；全局派发已暂停。两个 legacy timer disabled/inactive | issue #120、scheduler API 与 production task 读模型；当前不是已验收接管 |

## 版本与发布基线

| 项目 | 当前事实 | 证据 |
| --- | --- | --- |
| 源码版本 | protected main / 生产为 `0.6.1`；issue #120 分支正在准备 `0.6.2` 候选，尚未合并或发布 | release contract + active deployment manifest |
| 最新标签 | annotated `v0.6.1` → `aca73a045275908b4fd710df777f563fef673b90`；标签不可移动 | Git ref + protected-main Checks |
| v0.6.1 GitHub Release | **未完成**：tag-triggered run `34917000586` 在生成 receipt 时因空 CI run ID 失败；补证由 issue #120 跟踪 | GitHub Actions；不能以 activation pass 代替 Release receipt |
| 生产 deployment source commit | `aca73a045275908b4fd710df777f563fef673b90`；schema 5 | active deployment manifest + readiness |
| 基线规则 | 生产 deployment 只能由 commit-scoped `verify` 成功的 protected-main commit 创建；release 标签只打在该 commit 上且不可移动 | `docs/release-checklist.md` |

生产 deployment 已进入 v0.6.1，但调度器接管尚未验收。EURUSD canary 在 01:28 和 01:45 两轮把单纯 `coverage_not_ready` 重试至 dead letter；第二轮证明 fixed-delay 自动续轮有效，也证明缺口分类/冷却错误。01:51:39Z 已通过受审计 API 将账本全局派发位暂停，随后 queued/running 均为 0；原 terminal runs 保持不可变。修复与真实环境补验依据[接管补充 spec](specs/2026-09-15-scheduler-takeover-remediation-and-acceptance.md)和 issue #120。

2026-09-14 发布 `v0.5.1`（**仅打标签与发布 GitHub Release，不推生产**）：把 `v0.5.0` 之后合入的修复收口为补丁版——WebUI 跨工作区移交（#82/#83）、探索页提交语义（#84）、覆盖度"未计算"的原因（#85）、容量测量来源（#86）、验收 receipt 可判读（#94）、告警闸门收窄（#95）、生产配置来源守卫（#92/#93）。发布准备 PR #102 以 squash 合入为 protected-main 提交 `edc6c1c442e4960fdd82d08fd6db4da279d0e275`，其 commit-scoped `verify`（run `34804428044`）成功；本地统一门禁在与之树等价的 `d8aed91` 上 `result=pass`（`software_version=0.5.1`，276 passed / 4 skipped，浏览器验收 57 checks 双视口）。`Release` run `34804615895` 发布了 `v0.5.1` 与 `release-receipt.json`。

上述 v0.5.1 段落是历史发布事实；当前生产身份以本节表格和 active deployment manifest 为准。

2026-09-14 收口生产配置来源：host-local drop-in `provider-env.conf` 曾让 API/worker 额外读取 `market-data-center-latest/.env.local`（仓库 checkout）。它与机器级 env 的三个共有键（`DATACENTER_API_KEY`、`DATACENTER_PROXY_URL`、`FRED_API_KEY`）取值一致，另含一个生产不使用的 `GITHUB_TOKEN`，因此两个 drop-in 已移除，机器级 env 成为唯一配置来源；provider 通路不受影响（`DATACENTER_PROXY_URL` 仍在进程环境中）。移除后 `DropInPaths` 为空、进程环境不再含 `GITHUB_TOKEN`，`deployment_id`/`software_version`/`source_commit` 与 `v0.5.0` 基线保持一致（本次不涉及 release）。回滚副本保留在 `$HOME/market-data-center/config-history/2026-09-14/`，receipt 见 evidence root `operations/production_env_source_consolidation/`。约束不变：仍不得手工改动 immutable release 的 unit 或依赖，生产行为变更必须走 approval。

monitor timer 配置为 `OnUnitInactiveSec=60s`，但实测节奏为约 120s（systemd 默认 `AccuracySec=1min` 的合并效应），即告警分辨率实际减半；这是配置事实，不是故障。

## 生产任务与统一调度（已部署，接管补验中）

调度器代码和服务已进入生产，当前因 canary 缺口分类故障暂停真实派发；旧入口也保持停用。此状态保护证据与所有权，但不构成已验收的默认接管或已验证的 legacy 回退。

| 项目 | 当前事实 | 证据 |
| --- | --- | --- |
| 调度器单元 | installed/enabled/active；进程派发位开启，账本全局位暂停，因此有效派发为 false | systemd + `/operations/scheduler` |
| legacy 入口 | raw、derived timer 均 disabled/inactive；旧 derived service 历史状态 failed 且 runner 曾指向陈旧 checkout，恢复前必须验证身份和 scope | systemd + 补充 spec TA07 |
| 计划与范围 | 9 条均为 `fixed_delay 900`；EURUSD raw-only enabled，其余 8 条 paused。暂停计划含 5m 及多个更高周期，不得整体恢复来冒充首批 raw/5m 范围 | `/production/tasks` 读模型 |
| canary 结果 | manual 与 scheduled 两个 execution 均 failed，各产生一个 QualityError dead letter；全局派发随后暂停 | execution/run IDs 与 issue #120 |
| 影子证据 | 有计划影子窗口约 2h19m，未达到发布约定 ≥4h；原窗口只作为部分证据 | scheduler tick receipts；TA04 待补 |
| retention audit | 已独立运行并保留 `retention_audit` receipt，不再依赖 provider acceptance | `operations/retention_audit/` |
| 待完成 | v0.6.2 修复发布、v0.6.1 Release 补证、可用回退、连续影子、FX raw、crypto/5m、分批扩面和文档证据索引 | issue #120、TA01–TA10 |

恢复顺序以补充 spec 为准：保持派发暂停 → 交付新修复版本 → 验证可用 legacy 回退并补连续影子 → FX raw → crypto/5m → 逐批扩面。任一步异常停止推进并保留 receipt。

## Consumer / ownership 矩阵

| Consumer/数据域 | 当前路径 | 状态 | 回滚/边界 |
| --- | --- | --- | --- |
| macro-market-lab bar preview/query | Data Center HTTP adapter | 已默认切换 | `MACRO_MARKET_USE_DATA_CENTER_BARS=0` |
| macro-market-lab Dukascopy raw/derived maintenance | Data Center runner | 已切换 | `MARKETLAB_MARKET_BARS_BACKEND=legacy` |
| yfinance DXY/SPY/QQQ/TLT macro-daily | macro-market-lab legacy workflow | 未迁移 | 独立数据域 |
| economic PIT/current consumers | 现有 flag/legacy 路径 | 未完成全量切换 | 必须先完成 PIT parity |
| ASK/MID | 未采集 | 第一阶段非目标 | 需独立 identity/API/spec |

## WebUI 数据维护工作台（v0.6.1，deployed）

| 项目 | 当前事实 | 证据 |
| --- | --- | --- |
| 分支 | protected `main`（生产任务控制台由 PR #112 交付，v0.6.1 为当前部署） | GitHub + deployment manifest |
| 维护任务 | `POST /maintenance/plans` 无副作用预览 + `POST /maintenance/tasks` 统一 queued envelope；`/derive/runs`、`/economic/ingest`、`/quality/checks` 复用同一 contract | `backend/tests/test_maintenance_contract.py` |
| 只读校验运行 | `quality`/`parity` run 只记录 findings，不发布 canonical part、不产生 manifest | `test_quality_run_executes_as_a_verification_and_records_findings` |
| Runs 读模型 | kind/scope/时间筛选 + opaque cursor 分页；`/runs/{id}/detail` 投影 stage、window、retry chain、degraded 原因，不改写 terminal receipt | `test_run_list_filters_and_cursor_pagination`、`test_run_detail_projects_stage_windows_and_retry_chain` |
| findings 治理 | 稳定 `finding_id`、occurrence 计数、`open/acknowledged/resolved` 处理状态与运行结果分离 | `test_findings_support_structured_filters_and_state_transitions` |
| 写保护 | capacity critical 与 warning 下 >31 天 backfill 返回 507 并进入写审计；鉴权失败 401 | `test_capacity_critical_protects_writes_and_is_audited` |
| 浏览器验收 | 1440px 与 390px 覆盖 provider ingest、derive、parity、quality（degraded）、economic ingest（本地 provider fixture）、被拒写入与容量保护写入 | `acceptance-receipts/browser/receipt.json` |
| 部署状态 | 已激活 `v0.6.1`（`aca73a045275-c6470772`）；生产任务控制台可读，真实调度派发处于全局暂停 | v0.6.1 activation receipt + scheduler/task API |

2026-09-13T23:56Z 的 v0.5.0 生产只读走查继续作为历史证据；当前组件身份和调度状态以本页顶部的 v0.6.1 读回为准。

## 状态语义

`implemented` 表示代码/测试存在；`deployed` 表示进入 immutable release；`accepted` 表示有真实 receipt；`default cutover` 表示默认走新路径且有回滚；`not migrated` 表示明确仍走旧路径。

## 未完成事项

1. yfinance macro-daily 数据域迁移。
2. economic PIT/current consumer 全量切换。
3. Dukascopy 历史 provider gap 不补造；高周期完整历史覆盖不作为已完成条件。
4. 旧 maintenance runner 已能把 provider gap 汇总为 degraded；v0.6.1 生产任务路径尚未复用同一轮次语义，导致 issue #120 的两个 dead letter。v0.6.2 候选正在统一该行为，尚未真实验收。
