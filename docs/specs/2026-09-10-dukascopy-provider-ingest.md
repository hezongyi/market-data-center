# Dukascopy Provider Ingest Specification

日期：2026-09-10  
状态：in progress  
前置 spec：`2026-09-10-production-hardening-and-consumer-migration`、`2026-09-10-query-contract-and-scalability`、`2026-09-10-release-and-operational-sustainability`、`2026-09-10-deployment-and-observability-hardening`

## 目标

将 `dukascopy-python==4.0.1` 作为正式 historical market adapter 接入 Data Center，使 Dukascopy provider-native closed bars 通过现有 queue、worker、quality、immutable part、manifest、receipt、API readback 和 scheduled acceptance 链路发布。

Provider module 的 interface 保持 `fetch_bars(IngestJob) -> list[ProviderBar]`。Dukascopy instrument、interval、BID basis、HTTP timeout、proxy、provider inclusive end 和 Data Center 半开区间的差异全部隐藏在 adapter implementation 内，不泄漏到 worker 或调用方。

## 首期范围

- Dataset：`provider_bars.v1`。
- Source：Dukascopy chart `json3` endpoint，由 `dukascopy-python==4.0.1` 访问。
- Data kind：provider-native historical OHLCV bars，不使用 `live_fetch()`，不下载全量历史 ticks。
- Price basis：仅 `BID`，落地 `price_type=bid`。首期不声称 MID，也不把 BID 标记为泛化 `raw`。
- Interval：UTC 半开区间 `[start,end)`；adapter 必须过滤 library inclusive-end 返回的边界行。
- Initial symbols：六字符 base/quote canonical symbol，例如 `EURUSD`、`XAUUSD`、`BTCUSD`；adapter 转换为 `EUR/USD` 等 provider symbol。
- Initial asset classes：`fx`、`commodity`、`crypto`，由受治理的 ingest job 明确提供。
- Initial timeframes：`1m`、`5m`、`15m`、`30m`、`1h`、`4h`、`1d`。
- Near-live tail、ASK、MID、ticks、equity/ETF CFD symbol registry 和 canonical handoff 不在首期范围。

## 分支、发布与容量前置

- Dukascopy 的正式实现分支必须从包含 immutable `v0.2.0` release 基线的最新 protected `origin/main` 创建 clean worktree。2026-09-10 记录的基线为 `origin/main=ab6bfa74029a094bbad1381b258ff4e5744f5847`，其中 annotated `v0.2.0` 指向 `54f002580a5c72cdd3da57ef12ffa14ea0f6c8c4`。
- 当前 dirty checkout 只作为待移植实现的来源，不得整体提交、merge 或作为生产部署目录。只移植 Dukascopy adapter、registry、测试、contract、acceptance 和 dependency 变更，并保留 `v0.2.0` 已完成的 release workflow、三 Python constraints、backup v2、capacity、evidence 和 deployment 能力。
- 重建后必须重新生成或验证 Python 3.10/3.11/3.12 constraints，确认 `dukascopy-python==4.0.1` 及其 transitive dependencies 在三个 runtime 中均可安装并通过统一 backend CI。
- D3 生产验收必须运行在 `2026-09-10-deployment-and-observability-hardening` 定义的 immutable release 上，receipt 记录真实 `deployment_id`、`software_version` 和 `source_commit`；dirty checkout 或直接从 repository 启动的进程不能产生正式 D3 证据。
- 每次 D3/D4 操作前后都记录 capacity snapshot。Capacity 为 `critical` 时禁止任何新 Dukascopy ingest；为 `warning` 时只允许不超过 31 天、已明确估算数据量的 D3 或 parity 短窗口，禁止无人值守历史 backfill 和批量迁移。
- D4 大规模历史迁移只有在目标 canonical storage 恢复为 `ok`，或迁移到经过 capacity、backup destination 和 recovery 验证的新挂载点后才能启动。不得通过降低 warning/critical threshold 绕过门禁。
- 如果容量处理涉及删除、压缩、合并或归档已有 canonical part/manifest，必须另建 retention/archive spec；本 spec 不授权这些操作。

## Provider contract

### 时间与范围

- `IngestJob.start/end` 必须是 timezone-aware datetime，且 `end > start`。
- Adapter 转换为 naive UTC 后调用 library，并在 normalization 后再次执行 `start <= bar_ts < effective_end`。
- `effective_end` 不得超过当前 timeframe 的最后完成边界；未闭合 bar 不发布。
- 为限制 `fetch()` 全量 DataFrame 内存行为，单次 1m/5m/15m/30m 请求最多 31 天，1h/4h 最多 366 天，1d 最多 3,660 天。
- 更长窗口必须通过正式 backfill 分片产生独立 run、receipt 和 immutable parts。

### 请求与失败

- Proxy 优先读取 `DUKASCOPY_PROXY_URL`，其次读取 `DATACENTER_PROXY_URL`；未配置时允许直连。
- HTTP timeout 优先读取 `DUKASCOPY_REQUEST_TIMEOUT_SECONDS`，默认 30 秒。
- Adapter 为 library 的 HTTP 请求补充 timeout、proxy 和 `raise_for_status()`；provider URL/body 不进入 receipt 或错误响应。
- Library 内最多执行一次短重试，后续重试由 Data Center ledger/worker 管理。
- 空结果、字段缺失、无效 index、重复 timestamp、非升序 timestamp、NaN OHLC 和不支持的 symbol/timeframe 均为受控 ingest failure，不能发布空成功 receipt。

### 标准化与 identity

- `provider=dukascopy`，`symbol` 使用无分隔符 canonical symbol，`asset_class/timeframe` 来自已验证 job。
- `currency` 使用 quote currency；`price_type=bid`。
- `bar_ts` 与 `ingest_ts` 使用 UTC。
- `source_hash` 覆盖 provider、canonical symbol、timeframe、bar timestamp、BID basis、OHLCV；不得包含 ingest time。
- 首期 `provider_bars.v1` 主键仍为 `provider + symbol + timeframe + bar_ts`，因此同一 selector 不能混合 BID/ASK。

## Worker、API 与验收

- Connector registry 注册 `dukascopy`，普通 `/api/v1/ingest/runs` 与 LocalWorker 不增加 provider-specific 分支。
- Receipt 记录固定 connector version、deployment ID、source commit、capacity before/after、row count、min/max timestamp、input/output hash、quality summary、part 和 manifest。
- `/api/v1/bars?provider=dukascopy` 只读取已发布 manifest，不触发 provider 网络请求。
- Scheduled provider acceptance 增加 `EURUSD`、`fx`、`1d` 的短窗口 ingest 与 API readback。
- Backfill CLI 允许 `provider=dukascopy`；首期 CLI 继续使用 1d 分片，分钟级批量回补留给后续 bounded planner。

## 数据迁移

- 现有 `macro-market-lab` Dukascopy files 不因路径存在而自动成为 Data Center 已发布数据。
- D4 开始前先生成只读 inventory，至少按 symbol、asset class、timeframe、year、price basis 统计文件数、行数、min/max timestamp、估算 bytes、重复键和缺失区间；inventory 不写 Data Center manifest。
- 如需迁移历史 part，必须生成 Data Center manifest/receipt，并验证 schema、BID basis、半开区间、row count、hash、重复键和内部 coverage。
- Migration module 的 interface 接受已确认 inventory item 和 bounded window，内部负责读取 legacy BID data、normalization、quality、immutable publication 和 receipt；调用方不得直接复制 legacy Parquet 到 canonical 或自行生成 manifest。
- 迁移前后在固定窗口比较 row count、min/max、稳定 OHLCV hash、BID basis、duplicate count、coverage 和错误语义；差异必须产生显式 failed migration receipt，不能发布部分成功。
- Consumer 只有在 Data Center HTTP parity 通过后才切换；旧 reader 保留为回滚路径。Feature flag 默认关闭，切换和回滚分别生成包含 Data Center 与 `macro-market-lab` commit 的 receipt。
- Bulk migration 按可恢复的 bounded batch 执行，每批使用独立 run/receipt/manifest。Capacity 重新进入 warning 或 critical、readiness 失败、hash mismatch 或质量失败时，后续批次停止，已发布的 immutable batch 保留且不回滚删除。

## ASK 与后续 schema

- ASK 接入需要 `provider_bars.v2` 或等价的新 identity，把 `price_basis` 纳入主键、分区、selector、pagination cursor 和 coverage。
- BID/ASK 都存在后才能定义 spread evidence；OHLC 字段平均只能标记为 approximate MID，不能冒充 tick-path MID。
- Volume 在 provider-native bar、library ticks 和 JForex 之间完成单位/tolerance 验证前只参与非负检查，不作为 overlap hard gate。

## 不变量

1. Dukascopy 网络请求只能发生在 ingest adapter，query endpoint 永不触发 provider 请求。
2. 首期所有 Dukascopy rows 都是明确标记的 BID historical bars。
3. Provider inclusive end 不得破坏 Data Center `[start,end)` 语义。
4. 未闭合、空、重复、无序或 OHLC 缺失的数据不得发布。
5. Retry 创建新 attempt/run 状态，不覆盖 terminal receipt 或 canonical part。
6. Existing macro data 不经 migration gate 不进入 Data Center manifest visibility。
7. Dukascopy 正式分支从包含 `v0.2.0` 的 clean protected-main baseline 重建，不得回退 release、capacity、backup、evidence 或 deployment contract。
8. D3/D4 正式证据只来自 immutable deployment；开发 checkout 的 adapter smoke 不能替代生产验收。
9. Capacity warning/critical 不能通过调低阈值规避；D4 bulk migration 不得在 warning/critical 状态启动或继续。
10. Legacy inventory、parity 和 migration 只读或追加发布，不覆盖、移动或删除 legacy files 与既有 Data Center canonical parts。

## 实施阶段与门禁

### D0：Clean baseline reconstruction

- 从最新 protected `origin/main` 创建独立 clean branch/worktree，基线必须包含 immutable `v0.2.0` 及 deployment/observability hardening 的适用实现。
- 从当前实现中只移植 Dukascopy-specific adapter、registry、tests、contracts、acceptance 和 dependency 变更，不整体带入旧 branch 的 release 文件或版本号。
- 重新生成/验证 Python 3.10、3.11、3.12 constraints，并运行 Ruff、backend tests、dependency/compatibility checks、operations acceptance、Web build、browser acceptance 和 service acceptance。

门禁：工作区 clean；diff 不删除或回退 `v0.2.0` release artifacts；package/Web UI version 不低于 `0.2.0`；三个 Python runtime 的 locked install 和统一 CI 全部通过；protected-main 基线 commit、移植来源和最终 diff receipt 可追踪。

### D1：Adapter contract

- 固定 dependency、symbol/interval mapping、BID basis、timeout/proxy、half-open filter 和 normalized ProviderBar。
- 通过 fake provider adapter tests，不访问真实网络。

门禁：normal、inclusive-end、empty、invalid payload、range limit 和 proxy/timeout tests 全部通过。

### D2：Governed ingest

- 注册 connector，接入 worker、receipt、manifest、readback 和 compatibility check。
- 增加隔离 end-to-end ingest test。

门禁：从 queued run 到 API readback 的完整路径通过，manifest/receipt 显示 `provider=dukascopy` 和固定 connector version。

### D3：真实 provider acceptance

- 在 immutable deployment 上使用 `EURUSD`、`fx`、`1d` 短窗口执行正式 API → queue → worker → publication → API readback。
- 执行前后记录 capacity，验证 proxy/直连模式、requested/effective 半开区间、closed-bar cutoff、row count、min/max、BID basis、input/output hash、quality、part、manifest、deployment identity 和 paged/unpaged readback。
- 将 Dukascopy 加入 scheduled provider acceptance 和 runbook；至少保留一次人工触发成功和一次 timer 触发成功，且两次均使用正式 production configuration、隔离的 acceptance run scope 和有界窗口。
- 验证空结果、provider timeout/status failure 和无效 payload 在生产部署中形成安全失败 receipt，不泄漏 provider URL/body、proxy credential 或环境路径。

门禁：D0-D2 已在 clean baseline 重新通过；capacity 不为 critical；人工与 scheduled receipt 均包含 deployment ID、commit、环境、请求/effective 区间、实际 min/max、row count、BID basis、hash、quality、manifest/readback 和安全失败类别；受保护 `main` hosted `verify` 成功。

### D4：历史迁移与 consumer parity

- 生成 `macro-market-lab` legacy Dukascopy BID inventory、容量需求和 migration plan；在 capacity warning 时只运行不超过 31 天的只读/短窗口 parity，不启动 bulk publication。
- Capacity 恢复为 `ok` 或新目标挂载点通过验收后，按 bounded batch 执行 migration，每批生成独立 run、manifest 和 migration receipt。
- 对首期每种已迁移 symbol/timeframe 选择首、中、末代表窗口完成 legacy reader 与 Data Center HTTP 双读 parity，并验证分页 snapshot 稳定性。
- 完成 feature flag 切换、切换后抽样双读、readiness/latency 观察和回滚演练；旧 reader 在观察期内保持可用。

门禁：capacity 为 `ok` 或新挂载点满足同等门禁；迁移不覆盖任何 legacy file 或现有 canonical part；所有 batch terminal receipt 完整；row count、min/max、稳定 OHLCV hash、BID basis、coverage、duplicate count、分页结果和错误语义一致；Data Center 与 `macro-market-lab` 的受保护 main checks 成功；切换与回滚 receipt 可追踪。

## D3/D4 验收证据

### D3 evidence

- Clean baseline commit、Dukascopy feature commit、protected-main merge commit、hosted `verify` run 和 immutable deployment manifest；
- D3 操作前后的 capacity status、free ratio、requested bytes estimate 和是否允许执行的 policy decision；
- 人工与 scheduled `EURUSD`/FX/1d receipt，包含 request/effective range、row count、min/max timestamp、`price_type=bid`、connector version、input/output hash、quality summary、part、manifest、snapshot ID 和 API readback hash；
- Proxy/直连模式与 request timeout 的安全配置证明，不记录 proxy URL、credential、provider response body 或完整敏感路径；
- Empty、timeout/status、duplicate/out-of-order、missing OHLC 和 unsupported selector 的受控失败 receipt；
- 服务重启后相同 deployment identity 下的 readback，以及 rollback 到前一 release 后 Dukascopy 写入不可用但已有 immutable 数据仍可按兼容 contract 读取的证明。

### D4 evidence

- Legacy inventory 与 migration plan，包含 symbol/timeframe/year、BID basis、rows、bytes、min/max、duplicates、coverage gaps 和预估新增容量；
- Capacity 恢复或新挂载点验收 receipt，以及 backup destination、recovery drill 和 write/read readiness；
- 每个 bounded migration batch 的 source reference、run ID、row count、hash、quality、part、manifest、开始/完成时间和失败位置；
- 首、中、末固定窗口的 legacy/Data Center row count、min/max、稳定 OHLCV hash、coverage、duplicate count、分页 snapshot 和错误语义 parity；
- `macro-market-lab` feature flag 默认值、切换 commit、Data Center 与 consumer 双方 protected-main checks、切换后抽样和 rollback receipt；
- 观察期内 latency、失败率、capacity 变化和未迁移清单；任何未满足项保持 D4 `in progress`。

## 实现记录（2026-09-10）

## 基线重建记录（2026-09-11）

- 从 protected `origin/main` commit `c608b608b949d9f695d54c426366f645bf4c6b5a` 创建分支 `dukascopy-provider-ingest-20260911`，未整体带入旧 dirty worktree 的 release/deployment 改动。
- 逐文件移植 adapter、registry、依赖/constraints、scheduled acceptance、contract tests 和本 spec；主线 `0.2.0` 版本、deployment、capacity、snapshot、backup 与 CI 保持不变。
- 本地 py311 已完成锁定依赖安装；Dukascopy contract tests 5 passed，完整 backend tests 122 passed，统一 `bash scripts/ci.sh all` 通过。
- 本机 python3.10 缺少 `ensurepip`/`venv`，python3.12 未安装；三版本安装门禁和 hosted `verify` 留待环境可用后执行。当前结果不构成 D3/D4 正式生产验收。

- D1：新增 `DukascopyConnector`，固定 `dukascopy-python==4.0.1` 和 connector version `dukascopy-python-4.0.1-bid-v1`；支持六字符 canonical symbol、1m/5m/15m/30m/1h/4h/1d、BID basis、UTC 半开区间、closed-bar filter、bounded range、provider HTTP timeout/proxy/status validation 和稳定 source hash。
- D1：adapter contract tests 覆盖 inclusive-end 过滤、BID/currency normalization、proxy/timeout、分钟范围上限和重复 timestamp 失败语义。
- D2：connector 已注册到通用 market registry；现有 `run_fixture_ingest`、LocalWorker、manifest 和 query readback 无 provider-specific worker 分支即可处理 Dukascopy。
- D2：隔离 fake-provider ingest 验证 receipt、`provider_bars.v1` manifest 和 BID readback；compatibility check 验证 exact dependency pin。
- D3：2026-09-10 真实 adapter smoke 对 `EURUSD`、`1d`、`[2026-09-01,2026-09-04)` 返回 3 行，min/max 为 2026-09-01/2026-09-03，basis 为 BID。
- D3：同一窗口的隔离真实 API → queue → subprocess worker → immutable publication → API readback 成功，run `bde6d402-70ec-4b3e-ad0d-1b48012dd32d` 为 `pass`，receipt/readback 均为 3 行。该 run 位于临时 canonical root，不是生产数据或 consumer cutover 证据。
- Scheduled provider acceptance 已加入 Dukascopy EURUSD/FX/1d；仍需在 immutable deployment 更新后生成生产 receipt，并完成 D4 历史迁移与 consumer parity，故本 spec 保持 `in progress`。
- 2026-09-10 baseline audit 发现现有 Dukascopy 实现位于落后 `origin/main` 14 个提交的 dirty checkout；既有 D1/D2 测试结果作为移植输入保留，但必须完成 D0 并在 clean `v0.2.0`+ baseline 上重新验证后才能进入正式 D3。
- 2026-09-10 production storage 约 11% free，处于 capacity warning。D3 的不超过 31 天短窗口可在 policy 与 deployment 门禁通过后执行；D4 bulk migration 当前被容量门禁阻止。

## 非目标

- 不使用当前 brokerd historical API 批量建设多年历史；
- 不在首期下载或聚合全量 ticks；
- 不在 `provider_bars.v1` 中同时写入 BID 与 ASK；
- 不把 Dukascopy CFD 价格等同于交易所股票、ETF 或期货价格；
- 不因 existing path 中有 Dukascopy Parquet 就绕过 manifest、quality 或 receipt。
- 不通过整体提交当前 dirty checkout、回退 `v0.2.0` release 文件或从 repository 直接重启服务来完成 Dukascopy 部署。
- 不在 capacity warning/critical 状态执行无人值守 bulk migration，也不通过降低阈值、删除 canonical 历史或跳过 recovery evidence 释放门禁。
