# Data Center Market Data Platform Specification

日期：2026-09-11  
状态：implemented in development; controlled local acceptance passed; production activation pending

## 目标

建立可复用于多个 provider、asset class 和 consumer 的市场数据平台能力。平台负责标准化、覆盖规划、质量、不可变发布、派生、查询、审计和回滚；provider 特有行为由 adapter、capability 和 profile 表达。

## 分层

```text
Provider Adapter
  -> Raw Ingest Contract
  -> Coverage / Window Planner
  -> Governed Ingest Run
  -> Immutable Catalog + Snapshot
  -> Transform Recipe Executor
  -> Derived Dataset
  -> Query / Consumer / Parity
```

`provider_bars` 保存 provider 原始事实，`market_bars` 保存按业务 session 规则生成的派生事实。派生结果不得回写原始 dataset。

平台分为控制面和数据面。控制面保存 dataset definitions、provider capabilities、instrument metadata、maintenance policies、transform recipes、quality profiles 和 dependency graph；数据面保存 Parquet parts、manifests、snapshots，并执行 ingest、derive 和 query。每次执行先将控制面配置解析为 immutable execution plan，再交给 worker，避免各模块重复解释配置。

## 通用模块 interface

```text
fetch(job) -> ProviderBar[]
plan_maintenance(dataset, selector, coverage, policy) -> IngestWindow[]
execute_ingest(window, connector, run_policy) -> ingest receipt
derive(recipe, input_snapshot, selector, start, end) -> derived receipt
query(dataset, selector, snapshot, page) -> QueryPage
```

运行类型统一使用 `run_kind=ingest|derive|backfill|gap_repair|quality|parity`，与 `run_scope=production|acceptance|migration|maintenance` 分开记录。所有类型共享同一 receipt 和 lineage contract。

模块职责：

- connector：provider 请求、symbol 映射、标准化和安全 failure；
- capability/instrument registry：品种、asset class、currency、session、时间周期和价格语义；
- coverage evaluator：watermark、缺口、重复、合法闭市和预期覆盖；
- window planner：首次回补、tail 更新、gap repair、分片和断点；
- ingest orchestrator：queue、worker、staging、quality、原子发布和 receipt；
- quality engine：schema、identity、时间、OHLCV、price basis、coverage 和 drift；
- catalog/registry：dataset schema、partition、selector、snapshot 和 lineage；
- transform executor：按 recipe 读取 snapshot、计算、重算、发布和记录 lineage；
- maintenance/acceptance：调度、容量、重试、parity 和 rollback。

provider 名称不得出现在通用 worker、coverage planner、quality engine 或 transform executor 的业务分支中。差异必须来自 capability、instrument metadata、session/calendar profile 或 recipe 配置。

## Dataset、identity 和 lineage

每个 dataset 由一份 `DatasetDefinition` 在 registry 中登记：`dataset_id`、schema version、`kind=raw|derived`、primary key、partitioning、selector fields、required lineage、quality profile、query modes、retention policy 和 materialization policy。新增 dataset 不应要求在 manifest validator、query engine 和 API 中分别增加硬编码白名单。

派生 dataset 至少记录：`recipe_id/version`、`input_dataset`、`input_snapshot_id`、`source_timeframe`、`target_timeframe`、`price_basis`、session/calendar version、aggregation version、input/output hash。每个执行还记录 `recipe_digest`、`capability_digest`、`instrument_digest`、`session_profile_digest`、`calendar_digest` 和 `quality_profile_digest`，以区分代码、数据和配置变化。source hash 集合在 receipt/manifest 中使用 `source_hash_count`、`source_hash_digest`、`first_source_hash` 和 `last_source_hash` 的有界摘要，完整集合仍可从 immutable parts 重建。

未来 schema 应支持区分 `data_kind=provider_raw|derived` 和 `price_basis=bid|ask|mid|synthetic_mid`。现有 v1 不因本 spec 自动升级，但新数据不得通过隐式命名混淆这些维度。

## Recipe

Recipe 是不可变、可审计的配置对象，至少声明：输入/输出 dataset、source/target timeframe、允许 schema、selector 约束、session/calendar、OHLCV 规则、partial bucket policy、缺失输入策略、重算窗口和质量策略。

同一 recipe 必须能够作用于两个不同 provider 的标准化输入。语义不兼容时新增 profile 或 recipe，不复制整套 executor。

Recipe 还必须声明 `materialization=persisted|ephemeral`、`publication_policy=canonical|research_only`、依赖范围、受影响窗口计算方式和下游目标。正式 consumer 只能读取 persisted/canonical dataset；ephemeral 结果不得进入 catalog current state。

派生结果形成显式 dependency graph。输入 snapshot 修复或替换时，系统先生成下游重算计划，列出受影响的 target dataset、selector 和时间窗口，再由 operator 或受控 scheduler 执行；第一阶段不要求自动级联。

## Coverage 与 readiness

Coverage evaluator 必须分别输出 `physical_coverage`、`session_coverage`、`quality_status`、`readiness_status` 和 `latest_complete_boundary`。文件存在或 min/max 覆盖不等于 session 完整、质量通过或 consumer 可用。FX 闭市、金属 session、crypto 24x7 和股票交易时段通过 profile/calendar 解释。

## 不变量与门禁

1. query 只能读取已发布且 hash 校验通过的 snapshot。
2. raw part、derived part、manifest 和 receipt 不覆盖历史事实。
3. maintenance、quality、acceptance、parity 和 derived run 共享统一 run/receipt/lineage contract。
4. 派生结果必须能从 immutable input snapshot 和 recipe version 重建。
5. 新 provider 至少通过 connector、coverage、quality、maintenance 和 recipe contract tests。
6. 当前本地实现保持 Parquet、DuckDB、SQLite ledger 和 worker；不提前引入分布式任务平台或动态脚本引擎。
7. provider-specific 逻辑只能位于 connector、capability、metadata、profile 或 recipe 配置。
8. receipt、metrics 和 query 可以按 `run_kind` 与 `run_scope` 区分，不得混淆 ingest、derive、quality 和 parity。
9. 派生输出必须可由 input snapshot、recipe version 和配置 digest 重建。
10. canonical consumer 不得读取 ephemeral 或 research_only 输出。

## 验收

- fixture 加至少一个真实或受控 provider 通过 raw ingest contract；
- 至少两个 provider 复用同一 maintenance planner；
- 至少两个 provider 复用同一 transform executor；
- fixture、Dukascopy 和至少一个 24x7 provider（首选 Binance）完成 planner、quality 和派生 contract 验证；
- dataset registry、manifest、snapshot、query 和 lineage readback 可验证；
- 失败、重试、capacity、服务重启和 rollback 均有 receipt。

## 实施记录

- 控制面 registry 已覆盖 dataset definition、provider capability、session profile、quality profile、maintenance policy、recipe 和 dependency graph；capability 可区分 provider-native `timeframes` 与 canonical `maintenance_timeframes`。
- raw ingest、coverage/window planner、immutable manifest/snapshot、通用 transform executor、market-bars query 和统一 run kind/scope 已落地；派生读取使用 selector、时间范围和 Parquet predicate pushdown，并按 session profile 过滤。
- 本地完整测试：`PYTHONPATH=backend/src .venv/bin/python -m pytest -q`（当前 151 tests）；backend CI、web/browser/service acceptance 均通过。
- 受控隔离环境验收：`scripts/platform_acceptance.py` 对 fixture、Binance、Dukascopy 完成 raw readback、coverage/readiness、5m derive、lineage 和 recomputation-plan 检查；该证据不等同于 Dukascopy 全品种生产持续维护或 macro-market-lab 全量 cutover。
- 真实隔离环境复核：当前工作树已通过真实 Dukascopy `EURUSD 1m BID` 的
  `LocalWorker -> connector/network -> staged Parquet -> manifest -> snapshot` 链路，
  并完成同一 snapshot 的 `5m market_bars` readiness/readback。该结果仍不是生产部署证据：
  生产 systemd 当前运行旧 release `60112e7f35c95f71f430cedc04ee52e42070882e`，且
  全品种持续维护、定时调度、macro-market-lab raw ownership cutover 尚未验收。
