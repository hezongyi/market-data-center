# Market Bars Derivation and macro-market-lab Cutover Specification

日期：2026-09-11  
状态：derivation executor deployed; generic derived-maintenance runner implemented and bounded-plan tested; core-symbol 5m canary passed; 15m/30m/1h bounded multi-symbol acceptance passed; latest production 5m materialization and macro-market-lab read-only adapter acceptance passed; macro-market-lab default cutover pending
平台前置：`2026-09-11-data-center-market-data-platform`  
数据前置：`2026-09-11-dukascopy-1m-bid-rollout`

## 派生目标

将 provider raw bars 转换为可供研究和图表使用的 canonical `market_bars`。派生模块不绑定 Dukascopy；Dukascopy 1m BID 只是第一份 recipe。

默认依赖图：

```text
provider_bars 1m -> market_bars 1m/5m/15m/30m/1h/4h/1d
market_bars 1d  -> market_bars 1w/1mo
```

路由由 recipe registry 配置，不由 provider 名称硬编码。recipe executor 只读取已发布 input snapshot，按 session profile、calendar、trading date、partial bucket policy 和质量策略计算，再发布独立 immutable output。

每次派生先解析 `DatasetDefinition` 和 recipe，生成 immutable execution plan。recipe 必须声明 `materialization=persisted|ephemeral`、`publication_policy=canonical|research_only`、依赖范围和受影响窗口规则。正式 `market_bars` 使用 `persisted/canonical`；临时研究结果不得进入 current catalog。

`data_center.derived_maintenance_runner` 是统一运维入口。它按 recipe 依赖层逐层刷新
catalog snapshot，计算受影响的完整 bucket，使用同一 snapshot/recipe/window 做幂等检查，
然后通过 `/api/v1/derive/runs` 投递给标准 worker。`--dry-run` 只生成计划和 receipt；
实际运行不能绕过 API、ledger、staging 或 readiness 门禁。这样新增 provider 时只需注册
capability、instrument 和 recipe，不需要复制一套派生调度器。

## 派生 receipt 与 lineage

每个 derived run 必须记录 recipe/version、input snapshot、source hash 集合、source/target timeframe、session/calendar version、aggregation version、输入输出行数、时间范围、质量结果和 output manifest。不同 recipe、price basis、session policy 或输入 snapshot 的结果不得共享无法区分的 identity。

同时记录 `recipe_digest`、`instrument_digest`、`capability_digest`、`session_profile_digest`、`calendar_digest` 和 `quality_profile_digest`。1m 修复后先生成 dependency graph 的下游重算计划，明确受影响的 target dataset 和时间窗口，再执行派生；第一阶段允许人工确认重算计划。

## session 与质量

session boundary、late open、early close、holiday、内部缺口和不完整 bucket 必须由 profile/policy 决定。provider-native 高周期只作为对照资料，不作为 canonical 派生结果的默认输入。派生质量至少检查时间边界、bucket 完整性、OHLCV、重复主键、输入覆盖和 lineage 完整性。

派生状态必须区分 physical input coverage、session coverage、quality status、readiness status 和 latest complete boundary。只有 readiness 通过的 persisted/canonical 输出才能供正式 consumer 使用。

## `macro-market-lab` 迁移

迁移顺序固定为：

1. 对固定窗口做 Data Center `provider_bars 1m` parity；
2. 对相同 input snapshot 做 `market_bars` 多周期 parity；
3. 通过只读 HTTP adapter 切换 query/preview；
4. 观察期通过后切换派生 bars consumer；
5. 最后再评估 raw fetch、quality 和 maintenance ownership 的迁移。

Parity 至少比较 row count、min/max timestamp、trading date、OHLCV hash、coverage/gap、quality、错误语义和 lineage。分页与非分页读取必须一致。feature flag 默认在观察期前保持旧路径，rollback 只切回旧 reader，不删除 Data Center 数据。

## 泛化验收

先用 Dukascopy `1m BID` 注册 recipe，再用 fixture 或第二 provider 使用同一 executor 完成相同 contract test。新增 provider 时优先复用 recipe、session/calendar profile 和 quality rule；只有语义不兼容时才新增配置或 recipe。

## 完成门禁

- 派生 dataset 已加入 registry、catalog、manifest、snapshot 和 query contract；
- `DatasetDefinition` 登记 kind、identity、lineage、quality、retention 和 materialization policy；
- 固定 snapshot 的跨周期、跨 session parity 通过；
- 至少两个 provider 复用同一 transform executor；
- 至少一个 24x7 provider 与 Dukascopy 使用同一 recipe/ executor 完成 contract test；
- `macro-market-lab` 每个切换 consumer 有 parity receipt、观察期和 rollback evidence；
- 未迁移的 raw fetch、quality、maintenance consumer 仍有明确清单，不得误报为已切换。

2026-09-12 生产验证已完成 8 个 Dukascopy approved symbols 的固定窗口
`provider_bars 1m BID -> market_bars 5m` worker staging、immutable manifest、lineage、
readiness 和 HTTP query readback（每个窗口 12 根 5m bars）。在同一完整且已 ready
的 2026-09-10 12:00--13:00 UTC 1m snapshot 上，15m/30m/1h recipe 又完成 23 个
真实 acceptance runs（8 个 symbol，EURUSD 的 15m 已先行单独验证），全部 `pass`，
输出分别为每小时 4/2/1 根，均为 `price_basis=bid`，并写入独立 manifest/lineage。
这些证据证明派生 executor 的多周期和多品种复用，但不代表 4h/1d/1w/1mo 已具备
完整历史覆盖：完整工作日回补仍被真实内部缺口保护性拒绝，需先完成可用的 1m
coverage 后再做高周期 canonical materialization。

多周期汇总 receipt：
`/home/quant/market_lake/evidence/data-center/operations/dukascopy_derived_multiperiod_acceptance_v2/2026-09-12T035655.196737+0000-9de4316e5d6e437ba9322d5d815417ee.json`。
其中 5m 有 9 个已通过 production runs，15m/30m/1h 各有 8 个 acceptance runs，
总计 33 个真实派生 runs。

macro-market-lab 的只读 Data Center adapter 已用 `query preview dataset` 在 EURUSD
5m 上真实返回 Data Center 数据，包含 `source=data_center`、`price_basis=bid`、
recipe 和 input snapshot。当前 `.env.local` 已配置 flag-on 的本地运行入口，但
代码默认仍为 flag-off，consumer acceptance 已验证 flag-on、legacy rollback、分页
一致性和 60 秒失败率为 0；正式默认 cutover 仍需将该观察证据扩展到实际运行入口，
不能把本地环境变量配置等同于所有 consumer 已切换。

2026-09-12 的 consumer acceptance 已重新绑定 release `77979f3588b1-a626e764`：
flag-on source 为 `data_center`，flag-off 保持 legacy reader，Data Center 和 consumer
观察期失败率均为 0，readiness 为 `ready`。正式默认 flag 仍保持关闭，直到各实际运行
入口完成同等观察和 rollback 证据。

重新验收 receipt：
`/home/quant/market_lake/evidence/data-center/operations/dukascopy_consumer_parity/2026-09-12T043840.242152+0000-ab789afdab8a4992b32680cc5b56d9bb.json`。
该 receipt 的 `deployment_id` 为 `77979f3588b1-a626e764`，覆盖 flag-on、legacy
rollback、BID-only selector、分页快照一致性和 60 秒观察窗口。

在最新 Data Center deployment `d115c4a3bd71-fe6419a9` 上，已再次通过生产 worker
执行 EURUSD `1m BID -> 5m`（2026-09-10 12:00--13:00 UTC），生成 12 根
`market_bars`；HTTP `market-bars` readback 与 macro-market-lab 的
`query preview dataset --dataset market_bars` flag-on 读路径均返回相同 snapshot、recipe、
lineage 和 `price_basis=bid`。该证据确认派生链路已可生产消费，但不等同于将所有
macro-market-lab 入口的默认 flag 打开；默认 cutover 仍需按观察期和 rollback 门禁推进。
