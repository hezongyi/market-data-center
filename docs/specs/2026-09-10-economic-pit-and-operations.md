# Economic PIT and Operations Specification

日期：2026-09-10
状态：implementation and real-environment acceptance complete; hosted CI pending current PR
前置 spec：`2026-09-10-production-hardening-and-consumer-migration`

## 目标

在不破坏现有 provider-bars 和生产边界的前提下，完成 economic 数据的 point-in-time（PIT）契约，逐步迁移 `macro-market-lab` economic consumer，并把 CI、恢复、容量和告警能力变成可持续运营的门禁。

## 范围

### 1. Economic PIT 契约

冻结 `economic_observations.v2` schema，至少包含：

- `series_id`、`observation_date`、`value`、`frequency`、`units`
- `release_ts`、`asof_ts`
- `vintage_start`、`vintage_end`
- `availability_policy`、`availability_lag_days`
- seasonal adjustment、来源和缺失语义

未知的发布时间必须保持未知，不能用抓取时间伪装成 `release_ts`。current-state 和 PIT 查询必须分别定义可见性、排序、重复版本和无匹配结果的行为。

### 2. Consumer 迁移

economic consumer 只有在固定历史窗口完成双读 parity 后才允许切换。parity 至少比较行数、最小/最大时间、稳定 output hash、质量状态和错误语义。切换必须由显式 feature flag 控制，并保留旧路径作为回滚目标；ingest、quality 和 maintenance consumer 在各自契约完成前继续标记 `not_migrated`。

### 3. 持续运营

- hosted CI 必须对当前默认分支持续执行统一 CI 入口，并配置 required check。
- 增加 API/worker/ledger/canonical 数据的备份恢复演练，以及队列积压、并发 ingest、磁盘和网络故障测试。
- 告警支持可配置的 webhook 适配器、幂等事件 ID、失败重试和本地转存；事件必须能关联 `request_id`、`run_id` 和 receipt。
- 固定依赖升级和 connector/schema 兼容策略，处理现有 HTTP 测试弃用警告。

## 不变量

1. PIT 查询不得观察未来发布的数据，也不得把未知时间当作已知时间。
2. consumer 切换失败、延迟异常或 parity 不一致时，旧路径可立即恢复。
3. 备份恢复不得覆盖或重写 canonical part、manifest、receipt 和 terminal ledger 状态。
4. 告警失败不得改变数据写入结果；重复事件不得产生重复外部副作用。
5. 所有生产门禁必须有包含 commit、环境、命令、时间范围、结果和失败原因的证据。

## 分阶段门禁

### P1：PIT schema

完成 v2 schema、current/PIT API 契约、迁移样本和字段级测试。任何未知 release time 都有明确缺失语义。

### P2：Economic parity

在固定历史窗口对旧 consumer 和 Data Center 双读，生成 parity receipt；失败或不完整时保持 `not_migrated`。

### P3：受控切换

启用 feature flag，完成真实 HTTP 读取、延迟和错误预算验证，并演练回滚。保留旧路径和切换证据。

### P4：运营闭环

完成 hosted CI required check、备份恢复、容量/故障演练和告警出口验收。systemd 服务重启后仍保持 enabled/active。

### P5：依赖与版本治理

建立依赖升级检查、connector contract tests、schema/manifest/receipt 版本兼容矩阵，并清理已知弃用警告。

## 验收证据

- schema、API contract 和 PIT 查询测试全部通过；
- 至少一个固定历史窗口有成功和失败 parity receipt；
- economic consumer 切换与回滚各有真实 HTTP 验证；
- hosted CI 在最新提交通过且 required check 生效；
- 至少一次备份恢复和一次故障注入演练成功；
- 告警 webhook、幂等、重试和安全转存有测试证据；
- 依赖升级和版本兼容检查在 CI 中可重复执行。

## 实现记录（2026-09-10）

- P1：`economic_observations.v2` 已实现并成为新 FRED ingest 默认 schema；保留 v1 manifest 读取兼容，v2 对 provider 缺失值要求 `missing_reason`。API 增加显式 `mode=current|pit`，PIT 必须提供 `asof_ts`，并应用 `availability_lag_days`。
- P2/P3：`scripts/economic_parity.py` 对 PAYEMS（2025-05-01–2026-08-01）和 DGS10（2025-06-01–2026-08-01）生成成功 parity receipt，current-state 去重后的 row count、时间范围、稳定 hash、HTTP 延迟预算和错误预算一致；失败窗口也生成 `not_migrated` receipt。`macro-market-lab` commit `a1497e3` 通过 `MACRO_MARKET_USE_DATA_CENTER_ECONOMIC` 显式开关接入 economic preview 与 NFP economic loader，取消开关可回滚旧读取路径；NFP 事件读取保持 `bls_calendar` 优先，并用受治理的 `fred_release_calendar` 历史补齐缺失参考期。
- P3：生产环境启用 economic HTTP flag 后，NFP matrix 真实构建保留 10 个完整事件、生成 100 行 chart data；PAYEMS 与 DGS10 均从 `data-center://economic_observations/...` 读取。为完成真实交集，正式 Dukascopy raw-fetch/NY5 aggregation 将 EURUSD 补齐到 2026-09-09，Data Center worker 将 DGS10 补齐到 2026-09-08；2026-09-04 事件因验收时尚无完整 D0 close 被按契约剔除，不降低 `NFP_MIN_EVENTS=9`。
- P4：`scripts/operations_acceptance.py` 完成隔离 ingest、备份、恢复、字节校验和告警事件演练；生产 `retention-audit` 增加磁盘容量证据（当前 free ratio 约 11.7%，需纳入容量告警阈值评审）。systemd API/worker 重启后 readiness 通过，三个 timer 均 enabled；三 provider acceptance（Binance、yfinance、FRED）均通过，最新 receipt 为 `/home/quant/market_lake/evidence/data-center/economic-pit-20260910/provider-acceptance-final/receipt-43ff51329fb949a5a7bc8ce722100bb5.json`。
- P4：`docs/operations-runbook.md` 记录 readiness、PIT、parity、回滚、备份恢复和告警处置命令；本地 alert outbox 的 webhook 重试与稳定 `Idempotency-Key` 已有自动化测试。
- P4：GitHub 仓库已切换为 public，`main` branch protection 于 2026-09-10 通过 API 写入并回读成功：必须经 PR，required check 为 `verify`，strict up-to-date 为 true，管理员不可绕过，force push 与 branch deletion 均禁用。workflow `Checks` 保持 active，实际 job/check 名称为 `verify`。
- P5：共享 `scripts/ci.sh` 增加 `pip check`、依赖/契约兼容检查和运维演练；Data Center 本地完整 CI 为 61 tests、lint、secret scan、Web UI build、运维演练和隔离 service acceptance 全部通过。`macro-market-lab` 全仓 2,894 tests 与 `ruff check src tests` 全部通过。Starlette 的弃用警告通过 dev dependency `httpx2` 消除，当前 Data Center 本地仅保留 anyio 兼容警告。
- 最终本地/生产验收 receipt：`/home/quant/market_lake/evidence/data-center/economic-pit-20260910/final-local-acceptance.json`，包含两仓 commit、命令、生产数据覆盖、NFP matrix 输出、Data Center readiness、DGS10 ingest run 和 GitHub branch protection 回读结果；hosted CI 字段在当前 PR 通过前保持 `pending_current_pr`。

GitHub Actions API 已可读取，当前远端最近成功 run 为 `34421219129`（commit `c5a5c776ecac`），但它早于本 spec 实现提交。最终门禁只剩：将当前 feature branch 通过 PR 合入 `main`，确认受保护分支上的 `verify` 对本次提交成功；该 hosted run 取得前不得把历史 run 当作本次改动证据。

## 非目标

- 不在本阶段引入 Postgres、Redis、Kubernetes 或 RBAC；
- 不自动修复或删除 canonical 历史数据；
- 不在 PIT parity 未通过前迁移 economic、ingest、quality 或 maintenance consumer；
- 不把 hosted CI、provider SLA 或外部监控的可用性假设为本地代码保证。
