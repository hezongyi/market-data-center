# Dukascopy 1m Source and Derived Market Bars Specification

> 本文件为 2026-09-11 综合决策记录。正式实施规范已拆分为：
> `2026-09-11-data-center-market-data-platform.md`、
> `2026-09-11-dukascopy-1m-bid-rollout.md`、
> `2026-09-11-market-bars-derivation-and-macro-cutover.md`。
> 后续实现以三份拆分 spec 为准；本文件保留完整背景、讨论和历史决策。

日期：2026-09-11  
状态：approved for implementation  
前置 spec：`2026-09-08-data-center-foundation`、`2026-09-10-dukascopy-provider-ingest`、`2026-09-10-production-hardening-and-consumer-migration`

## 决策摘要

Dukascopy 在 Data Center 中采用 raw-first 路线：`1m BID` 是第一阶段唯一正式维护的原始行情源；其他周期由后续派生层根据 session profile 和交易日历生成，不把 Dukascopy provider-native 高周期作为 canonical 派生上游。

第一阶段不实现 ASK，也不独立抓取 MID。ASK 只有在成交执行、点差、滑点或买卖价回测明确需要时才进入单独的 schema/API 设计；MID 在具备 BID 和 ASK 后作为有明确 lineage 的派生数据处理，不能把 BID OHLC 或两组已聚合 OHLC 的简单平均标记成真实 tick midpoint。

本 spec 只定义目标契约和分阶段门禁。当前已有 `DukascopyConnector`、BID provider acceptance 和短窗口正式数据不代表 1m 全品种持续维护已经完成。

## 目标

1. 在 Data Center 建立可持续运行、可审计、可恢复的 Dukascopy `1m BID` 原始数据维护链路。
2. 保证每个原始 run 经过 connector、quality、immutable part、manifest、receipt 和 coverage/readback 后才可查询。
3. 为后续 `1m -> intraday/1d -> weekly/monthly` 派生建立稳定输入、lineage 和 session 语义，并使派生模块可复用于其他 provider。
4. 在不绕过 Data Center 治理的前提下，将 `macro-market-lab` 从旧 raw fetch/aggregation 路径分阶段迁移为只读 consumer。
5. 明确 ASK/MID 的非目标和未来扩展条件，避免在 v1 identity 不足时混写数据。

## 通用模块化原则

本 spec 不把目标定义成“Dukascopy 1m 到若干周期”的专用脚本，而是把 Dukascopy 作为第一条符合标准的 provider 数据线路。Data Center 的通用模块负责治理、规划、发布、派生和读取；provider adapter、instrument metadata、capability 和 session/calendar profile 负责表达外部差异。

目标逻辑链为：

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

通用模块必须保持小而稳定的 interface，复杂性集中在实现和配置中，不得让每个 provider 在 worker、聚合器或 consumer 中产生分支。

### 通用模块职责

| 模块 | 统一职责 | 允许的 provider/profile 差异 |
| --- | --- | --- |
| Provider connector | `fetch_bars(IngestJob) -> ProviderBar[]`、标准化字段和安全 failure | URL、认证、symbol 映射、分页、offer side |
| Request policy | timeout、proxy、retry、HTTP status 和错误分类 | 限流、特殊重试码、请求窗口 |
| Instrument/capability registry | symbol、asset class、currency、session、source timeframe、price basis | provider 品种元数据和能力声明 |
| Coverage evaluator | watermark、已覆盖区间、重复、缺口、预期 bar 数 | session、calendar、合法闭市规则 |
| Window planner | 首次回补、tail 更新、gap repair、分片和断点 | provider 最大范围、数据密度和容量估算 |
| Ingest orchestrator | queue、worker、staging、quality、原子发布和 receipt | 不应存在 provider 名称分支 |
| Quality engine | schema、identity、时间、OHLC、数值、basis、coverage 规则 | provider 特殊字段或阈值 |
| Dataset/catalog registry | schema、partition、selector、lineage、snapshot 解析 | dataset 版本和字段定义 |
| Transform recipe executor | 读取 immutable snapshot、计算、重算、发布和 lineage | recipe/profile 配置，不写 provider 分支 |
| Maintenance runner | 定期更新、回补、重试、状态、容量和审计 | maintenance manifest 和调度策略 |
| Acceptance/parity | 行数、时间范围、hash、质量和错误语义比较 | consumer 的比较字段 |
| Consumer adapter | 只读 HTTP、分页、feature flag 和 rollback | consumer 业务转换 |

新增 provider 时，优先复用这些模块；只有语义确实不兼容时，才新增 capability、session profile、quality rule 或 recipe。不得为每个 provider 复制一套 worker、coverage、aggregation 或 receipt 实现。

### 不应过早抽象的内容

当前 Data Center 采用本地 Parquet、DuckDB、SQLite ledger 和 worker。第一阶段不引入分布式任务平台、多数据库存储抽象或通用动态脚本引擎。抽象应停留在真实存在的变化点：已有多个 provider connector，已有多个 session/profile，未来会有多个 transform recipe；尚未发生的复杂变化不提前建框架。

## 当前基线与缺口

- connector 当前固定调用 `dukascopy_python.OFFER_SIDE_BID`，版本为 `dukascopy-python-4.0.1-bid-v1`。
- connector 能处理 `1m/5m/15m/30m/1h/4h/1d` provider-native bars，但 Data Center 当前没有派生聚合 workflow。
- `provider_bars.v1` 的 identity 为 `provider + symbol + timeframe + bar_ts`；虽然模型有 `price_type`，但主键、分区、selector、cursor 和 coverage 没有包含价格方向，不能安全混写 BID/ASK。
- 当前正式 manifest 可见的 Dukascopy 数据仍是有限验收窗口；legacy raw Parquet 不在 Data Center manifest 中，也不能因路径存在而视为已受治理数据。
- `macro-market-lab` 已有 session-aware 聚合实现，但其维护、raw fetch 和 aggregation 尚未整体迁移到 Data Center。
- Data Center 的通用 backfill/acceptance 仍需增加 1m 专用的分片、watermark、gap repair 和分钟级验收逻辑；不能直接把现有 1d 任务参数改名后视为完成。

## 范围

### 第一阶段：1m BID 原始层

- Dukascopy FX、commodity、crypto 中经批准的 symbol 集合。
- 统一 canonical symbol、UTC 时间、半开区间 `[start, end)` 和 closed-bar 规则。
- 有界抓取、增量 watermark、尾部更新、内部 gap 检测、缺口回补、幂等重试和运行审计。
- Parquet immutable parts、run manifest、quality summary、source/output hash、coverage 和 API readback。
- 维护任务只调度 `timeframe=1m`。connector 的 provider-native 高周期兼容能力可以暂时保留，但不得作为本阶段维护真相。

### 第二阶段：派生层

- 从受治理的标准化 `provider_bars` 生成 `market_bars` 或等价独立派生 dataset；派生实现不绑定 Dukascopy。
- 支持 `1m -> 5m/15m/30m/1h/4h/1d`，再支持 `1d -> 1w/1mo`。
- 保留 session profile、trading date、输入 snapshot/source hash、聚合版本和 partial bucket policy。
- 派生数据不得回写或覆盖 `provider_bars` 原始 part。

### 第三阶段：consumer cutover

- `macro-market-lab` 通过只读 HTTP adapter 读取 Data Center 数据，不读取 Data Center 的 Parquet、SQLite 或 provider 响应。
- 先切换低风险 provider-bars preview/query，再切换聚合后的 market-bars consumer。
- 每个 consumer 独立完成 parity、观察期和 rollback。

## 非目标

- 第一阶段不实现 ASK 原始采集。
- 第一阶段不实现独立 MID provider fetch。
- 不将 legacy `price_type=raw` 自动重标为 BID、ASK 或 MID。
- 不直接采用 Dukascopy provider-native `5m/15m/30m/1h/4h/1d` 作为 canonical 派生结果。
- 不为每个 provider 复制一套聚合实现；provider 差异必须通过标准化字段、session/calendar profile 和 recipe 配置表达。
- 不在查询请求路径触发 Dukascopy 网络请求。
- 不在 Data Center 1m 覆盖、质量和派生 parity 完成前关闭 `macro-market-lab` 旧维护路径。

## 1m 原始数据契约

### Selector 与价格语义

第一阶段所有 Dukascopy 原始行必须满足：

```text
provider=dukascopy
timeframe=1m
price_type=bid
```

`price_type=bid` 是可验证的来源语义，不使用泛化的 `raw` 代替。symbol、asset class、quote currency、session profile 必须来自已批准的 instrument metadata。

### 时间与边界

- `start` 和 `end` 必须是 timezone-aware UTC datetime，且 `end > start`。
- `effective_end` 不得超过当前 1m 完成边界；未闭合分钟不得发布。
- provider 的 inclusive end 必须在 adapter 内转换为 Data Center 半开区间 `[start, end)`。
- 维护 planner 必须使用分钟 watermark 和实际 coverage，而不是只按自然日推进。

### 分片与内存

- 单次请求仍受 connector 的有界范围限制，最长不超过 31 天。
- 维护 planner 应使用周级或动态更小分片，依据预计交易分钟数、响应时长和失败重跑 blast radius 调整；每个分片形成独立 run/receipt/part。
- `dukascopy-python` 的单页 `limit=30000` 不等同于整个 fetch 的总行数上限；但 fetch 会在内存中累积结果，小分片仍是默认运维策略。
- 超过单分片范围的历史回补必须通过正式 backfill planner 拆分，不得由一个无人值守请求承载。

### 质量门禁

发布前至少检查：

- 非空、DatetimeIndex、UTC、严格升序、无重复 timestamp；
- OHLC 非空且满足 high/low 一致性；
- `provider/symbol/timeframe/bar_ts` 主键不重复；
- `price_type` 全部为 `bid`；
- 目标区间外行被过滤；
- session-aware coverage、尾部缺口和内部缺口有明确结果；
- provider 请求错误、超时、HTTP status、空响应和无效 selector 以受控 failure 结束，不生成空成功 receipt。

FX 周末、节假日和品种 session 造成的非交易时段不能简单按全天每分钟连续性判定；gap 检查必须使用 instrument/session policy，并将真实内部缺口与合法闭市区分开。

## 维护与运行契约

1. planner 从已发布 manifest/coverage 解析 watermark 和缺口，生成明确的 1m `IngestJob`。
2. 每个分片通过 API/ledger/worker 执行，不能直接写 canonical。
3. 成功分片先写 staging，完成 schema、quality、hash 和 manifest 校验后再原子发布。
4. 重跑相同区间必须幂等；已发布 part 字节和 hash 不得被覆盖。
5. 失败必须记录 `error_type`、`failure_stage`、`retryable` 和失败区间，但不得持久化 provider URL、响应正文或 credential。
6. capacity 为 `warning` 时只允许明确估算且不超过门禁的短窗口；为 `critical` 时禁止新的 Dukascopy ingest。
7. 定期维护至少输出：覆盖起止、实际 row count、缺口数量、重复数量、最近 watermark、失败分片和容量状态。

### 通用维护接口

维护 runner 应通过数据集和 policy 驱动，而不是通过 provider 专用命令驱动：

```text
plan_maintenance(dataset_id, selector, policy, coverage_snapshot)
    -> IngestWindow[]

execute_maintenance(windows, connector, run_policy)
    -> maintenance receipt
```

`provider_bars` 的 Dukascopy 1m policy 是第一个实现；未来其他 provider 可以复用相同 planner 和 runner，只替换 connector、capability、session/calendar profile 与窗口估算。

## 泛用化派生模块

派生能力属于 Data Center 的通用 transform module，Dukascopy 只是第一套输入 recipe。模块的外部 interface 应保持为一个可审计的 recipe 执行请求，而不是暴露 provider-specific 方法：

```text
derive(recipe_id, input_snapshot, selector, start, end) -> derived run receipt
```

recipe 至少包含以下字段：

- `recipe_id` 和不可变 `recipe_version`；
- `input_dataset`、`source_timeframe`、允许的 input schema versions；
- `output_dataset`、`target_timeframe` 和 output schema version；
- selector 约束：`provider`、`asset_class`、symbol universe、price basis；
- `session_profile`、trading calendar、timezone 和 session override policy；
- OHLCV aggregation rule、partial bucket policy、quality policy；
- 依赖关系、重算窗口、输入 snapshot 选择规则和失败/重试策略。

同一 recipe 可以作用于 Dukascopy、Binance 或未来其他 provider，只要 provider adapter 产出的 `provider_bars` 满足相同 schema 和时间语义。provider-specific 逻辑只能存在于 raw connector 或 metadata resolver，不得散落在 aggregation implementation、worker 或 consumer 中。

### Dataset 与 catalog 要求

Data Center 应将派生数据作为 registry 中的一等 dataset，而不是隐藏在脚本目录或 consumer 本地：

- registry 登记 `market_bars`/`derived_bars` 的 schema version、partitioning、可查询 selector 和 lineage required fields；
- catalog path resolver、manifest validator、snapshot resolver 和 query engine 通过 dataset registry 工作，不为 Dukascopy 硬编码路径；
- 派生 manifest 除通用 run 字段外，必须记录 `recipe_id/version`、`input_dataset`、`input_snapshot_id`、输入 source hash 集合、`source_timeframe`、`target_timeframe`、session/calendar profile 和 aggregation implementation version；
- output identity 必须包含足以区分不同 recipe、price basis、session policy 和输入 snapshot 的字段，不能只依赖 provider/symbol/timeframe；
- 同一 selector 的新派生结果通过新 immutable part/manifest 发布，current-state 由 catalog 按明确 identity 解析，不覆盖历史事实。

### Provider 接入规则

新增 provider 时只需完成三类工作：

1. 实现 raw connector，产出符合 `provider_bars` contract 的数据并提供 provider capability/metadata。
2. 为该 provider/asset class 选择或新增 session/calendar profile，并声明允许的 source timeframe 与 price basis。
3. 将已有 recipe 绑定到 capability；只有语义不兼容时才新增 recipe，不复制整个 aggregation engine。

若 provider 的交易时段、volume 语义、价格 basis 或日界线与现有 profile 不兼容，应新增 profile 或 recipe 配置并完成独立 parity，而不是在通用聚合代码中加入 provider 名称分支。

### 通用身份与 lineage

所有 raw 和 derived dataset 都必须能回答四个问题：数据来自哪个 provider、采用什么价格语义、由哪个输入 snapshot 生成、使用哪个 recipe/version。未来 schema 应将 `price_basis`、`data_kind`、`source_timeframe`、`recipe_id/version`、`input_snapshot_id` 和 `calendar/session version` 作为可扩展的一等 lineage 维度；当前 `provider_bars.v1` 不因本 spec 自动改变，但新 dataset 不得继续依赖无法区分这些事实的隐式命名。

## 派生层契约

第一份 Dukascopy recipe 的输入只能是已发布、可定位 snapshot 的 `provider_bars 1m BID`；通用模块本身不把 `1m` 或 `BID` 写死，后续 recipe 可以声明其他合法 source timeframe/price basis。输出应使用独立 dataset（推荐 `market_bars`），并至少记录：

- `provider`、`asset_class`、`symbol`、目标 `timeframe`；
- `bar_ts`、`trading_date`、`session_id`；
- OHLC、volume、currency 和明确的 price basis；
- `source_timeframe=1m`、输入 snapshot ID/source hash；
- `recipe_id/version`、`aggregation_version`、session profile、交易日历版本；
- `input_snapshot_id`、输入 source hash 集合和 input/output row lineage；
- partial bucket 的处理结果和质量摘要。

聚合规则固定为：

```text
provider_bars 1m -> market_bars 1m/5m/15m/30m/1h/4h/1d
market_bars 1d -> market_bars 1w/1mo
```

上述是首个 Dukascopy recipe 的默认路由，不是全局硬编码路由。其他 provider 可以复用相同路由，也可以根据 capability 声明不同 source/target 组合，但都必须通过 recipe registry 和相同的 manifest/lineage interface。

session boundary、late open、early close、holiday、缺口和不完整 bucket 必须由显式 policy 决定，不能由调用方临时猜测。派生结果与 provider-native 高周期不做无条件等价声明。

## ASK/MID 后续决策

### ASK

只有出现以下明确需求时才启动 ASK：执行价模拟、买卖价差研究、滑点建模、bid/ask 回测或需要 spread quality evidence 的生产 consumer。

ASK 不能直接写入当前 `provider_bars.v1`。至少需要 `provider_bars.v2` 或等价的新 identity，将 `price_basis=bid|ask` 纳入：

- 主键和 source hash；
- manifest 分区和 snapshot selector；
- API 参数、分页 cursor 和 coverage；
- quality checks、acceptance receipt 和 consumer parity。

BID 与 ASK 同时维护时，必须增加 timestamp 对齐、缺侧、异常 spread、负 spread 和价格漂移检查。抓取和存储成本预计接近翻倍，必须先通过 capacity review。

### MID

Dukascopy 没有独立 MID offer side。MID 不作为第一阶段 provider 原始数据采集目标。未来可在同一 1m 时间窗对齐 BID/ASK 后生成 `synthetic_mid` 或 `approx_mid`，并记录：

- 两侧输入 snapshot/source hash；
- 对齐窗口和容差；
- 缺侧处理；
- 派生方法和版本。

对两组 OHLC 逐字段平均只能表示 approximate MID，尤其不能声称还原 tick-path 的 high/low。若需要真实 midpoint 路径，必须另立 tick 数据 spec。

## `macro-market-lab` 迁移门禁

### Gate 1：1m 原始 parity

对每个首批核心 symbol，在固定窗口比较旧路径与 Data Center：row count、min/max timestamp、重复/缺口、OHLC 稳定 hash、price type、质量状态和错误语义。

### Gate 2：派生 parity

在相同 1m snapshot、session profile 和日期范围上比较 `1m -> target` 的 row count、trading date、时间边界、OHLCV、质量摘要和 lineage hash。provider-native 高周期不作为 parity 目标。

### Gate 3：consumer cutover

- 使用显式 feature flag；默认值在观察期前保持旧路径。
- 至少完成连续多次重复读取、分页/非分页一致性、错误语义和 readiness 检查。
- 观察期内 failure delta 为零且无未解释 coverage drift 后，才允许扩大范围。
- rollback 只切回旧 reader/flag，不删除 Data Center canonical 数据。

在 Gate 1/2 未完成前，`macro-market-lab` 的旧 raw fetch 和 aggregation maintenance 继续保留；“支持 Data Center 查询”不等于“默认全量切换”。

## 实施阶段与交付物

### S1：1m contract 与 planner

- 固化 1m BID job contract、approved symbol manifest、watermark 和 session-aware coverage 模型。
- 实现分钟级分片、增量、缺口回补、幂等和动态失败重试。
- 抽出可复用的 coverage evaluator、window planner、maintenance runner；增加 1m connector/worker/quality/receipt contract tests。

门禁：固定 fixture 与受控 provider 响应下，边界、重复、乱序、空响应、超时、缺口和重跑测试通过。

### S2：核心品种生产维护

- 为批准的 FX、commodity、crypto 核心 symbol 生成分片回补和定期 tail maintenance receipt。
- 通过 capacity、服务重启、readback、coverage 和 gap audit。

门禁：每个 symbol 有连续维护证据，没有未解释的内部缺口或重复主键，容量策略未被绕过。

### S3：Data Center 派生层

- 实现 provider-agnostic 的 recipe registry、派生 executor、独立 `market_bars`/`derived_bars` dataset、catalog/query 支持和 session-aware aggregation。
- 先注册 Dukascopy `1m BID` recipe，再用 fixture/Binance 等第二 provider 验证相同模块可以复用。
- 固化 recipe/version、aggregation version、session profile、输入 snapshot 和质量/lineage contract。

门禁：固定 snapshot 的多周期 parity、跨 session 边界测试通过，并至少有两个 provider 使用同一派生实现完成 contract test；派生 dataset 已登记到 catalog/manifest/query contract。

### S4：consumer cutover

- `macro-market-lab` 先切只读 1m/query，再切派生 bars consumer。
- 每个 consumer 独立保留 parity receipt、观察期和 rollback 证据。

门禁：无未解释 hash/coverage 差异，feature flag、readiness、告警和回滚演练通过。

### S5：ASK/MID 评审（可选）

只有执行/点差需求被确认、容量允许且 v2 identity/API 设计完成后，才启动 ASK。MID 作为双边数据派生项另行验收；tick midpoint 另立 spec。

## 不变量

1. `provider_bars` 只保存 provider 原始数据，派生结果不回写该 dataset。
2. 第一阶段 Dukascopy canonical rows 的 `price_type` 必须为 `bid`。
3. 任何 query 只能读取已发布且通过 manifest/hash 校验的 snapshot。
4. 维护 planner 不得把 provider-native 高周期误当成 1m 派生结果。
5. 任何 consumer 在 parity、观察期和 rollback 准备完成前不得默认切换。
6. legacy raw 文件不因物理存在而获得 Data Center provenance。
7. ASK/MID 不得以修改 v1 默认值的方式偷偷进入现有 selector。
8. 派生实现不得通过 provider 名称分支表达 session、calendar 或 OHLC 规则；这些差异必须来自 recipe/profile/metadata。
9. 派生输出必须能够从 immutable input snapshot 和 recipe version 重建。
10. maintenance、quality、acceptance、parity 和 derived run 应复用统一的 run/receipt/lineage contract，不能各自发明不可互认的状态格式。
11. provider-specific 逻辑只能位于 connector、capability、instrument metadata、session/calendar profile 或 recipe 配置中。

## 完成定义

本 spec 只有在以下证据全部存在时，才能将 S1/S2 标记完成：

- 1m 分片 planner、增量 watermark、gap repair 和幂等重跑均有自动化测试；
- 至少一个核心 FX、commodity、crypto symbol 通过真实或正式受控 provider maintenance；
- receipt 包含请求/实际区间、row count、min/max、price type、coverage/gap、quality、part、manifest、snapshot 和 readback hash；
- 失败、超时、空响应、重复、乱序和 capacity gate 均有受控证据；
- 服务重启后仍可 readback，canonical 历史 part 字节不变；
- `macro-market-lab` 的旧维护路径仍有明确清单，未迁移项没有被误报为已切换。

S3 额外要求：派生 dataset 已登记到 catalog/manifest/query contract，且至少两个 provider 通过同一通用 executor 的 recipe contract test；不能只以 Dukascopy 单 provider 的聚合成功作为泛用化完成证明。

S1/S2 额外要求：coverage/window planner 和 maintenance runner 至少用两个不同 provider 的 fixture/capability profile 完成 contract test，证明分片、watermark、gap repair、capacity 和 receipt 逻辑没有绑定 Dukascopy。

S3/S4 和 S5 必须分别完成对应门禁后才能标记完成，不因 S1/S2 通过而自动宣称派生迁移、ASK 或 MID 已完成。

## 回滚与证据保留

- 代码回滚使用版本化 commit；数据回滚通过禁用新 manifest/consumer flag，不删除已发布原始 part。
- 每个维护分片、派生 run、parity 和 cutover 都保留不可变 receipt，并记录 commit、环境、输入 snapshot、命令、时间和结果。
- 证据沿用 Data Center 受保护 evidence 根目录和既有 retention policy；不得只保留口头或日志摘要。
