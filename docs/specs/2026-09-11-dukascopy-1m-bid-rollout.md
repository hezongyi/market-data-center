# Dukascopy 1m BID Rollout Specification

日期：2026-09-11  
状态：S1 implemented; S2 complete under provider-verified-gap policy; protected-main production deployment and real-environment acceptance passed; continuous maintenance active
平台前置：`2026-09-11-data-center-market-data-platform`

## 决策

Dukascopy 第一阶段只维护 `1m BID` provider raw bars。connector 的原生高周期能力可以保留兼容，但维护真相固定为 1m；5m 及以上周期交给平台派生 recipe。

## 原始契约

所有正式行满足：

```text
provider=dukascopy
timeframe=1m
price_type=bid
```

使用 canonical symbol、quote currency、UTC、半开区间 `[start,end)` 和 closed-bar 规则。请求、响应和错误细节隐藏在 connector 内。

## 维护链路

维护 runner 使用平台的：

```text
plan_maintenance(dataset, selector, coverage, policy) -> windows
execute_ingest(windows) -> receipts
```

本 rollout 的任务使用 `run_kind=ingest|backfill|gap_repair`，`run_scope=production|acceptance|maintenance`；执行前由平台控制面解析 `DatasetDefinition`、Dukascopy capability、instrument metadata、maintenance policy、quality profile 和 session/calendar profile，生成 immutable execution plan。

必须支持首次回补、分钟 watermark、tail 更新、内部 gap repair、幂等重跑、失败重试和 capacity gate。单次窗口最长不超过 connector 的 31 天限制；默认使用周级或动态更小分片，降低内存、超时和重跑影响范围。每个分片拥有独立 run、part、manifest 和 receipt。

覆盖检查必须理解 FX 周末/节假日、金属 session 和 crypto 24x7。合法闭市与真实内部缺口必须分开报告。

维护 receipt 必须分别记录 physical coverage、session coverage、quality status、readiness status 和 latest complete boundary，并保存相关配置 digest。min/max 文件范围不能单独作为维护成功依据。

## 质量门禁

发布前检查非空、UTC、升序、无重复 timestamp、OHLC 完整性、high/low 一致性、主键、`price_type=bid`、区间边界、session-aware coverage 和 provider failure。空响应、超时、HTTP failure、无效 selector 和质量失败不能产生成功 receipt。

## Coverage 与 ready 区间语义

历史 1m 数据不要求在每个自然分钟都有报价，也不能把从未请求/观察过的历史跨度推断
成 provider 缺口。`ready` 针对一个明确的半开请求区间
`[start,end)`，表示该区间内所有应交易的分钟都存在、顺序/主键/质量检查通过；它不
表示该品种从历史起点到最新时刻没有任何缺口。合法闭市分钟不计入应交易分钟。

当一段历史包含真实且已由 provider 原始响应复核的缺口时，整体 coverage 标记为
`degraded`，同时返回 `ready_intervals`：每个元素是没有缺失分钟的连续半开区间。下游
查询或派生只应选择完全落在某个 `ready_intervals` 内的范围；跨越缺口的范围必须保持
`not_ready`/失败，不得用前值、插值或 synthetic bar 填补。没有任何可用连续区间、存在
重复/结构性质量错误或 provider 请求失败时，状态为 `not_ready`。

| 状态 | 含义 | 下游动作 |
| --- | --- | --- |
| `ready` | 当前请求区间完整且质量通过 | 可查询、可派生 |
| `degraded` | 数据集中存在已解释缺口，但仍有可消费的 `ready_intervals` | 只消费 ready 区间；展示缺口 |
| `not_ready` | 没有可安全消费的区间或存在结构性错误 | 不发布、不派生 |

coverage API 不带 `start/end` 时只返回物理 `min_ts/max_ts` 摘要，不计算跨未观察历史的
缺口；要获得 `readiness_status`、`gap_count` 和 `ready_intervals`，必须传入明确的
`start` 与 `end` 请求范围。

## 品种与 rollout

先建立 approved symbol/capability manifest，优先覆盖 FX、commodity、crypto 核心品种。每个品种需要固定窗口的 coverage、gap、duplicate、OHLC/hash、重启 readback 和容量证据。legacy `price_type=raw` 文件没有可验证 BID provenance，不自动迁移或重标。

## ASK/MID 非目标

第一阶段不采集 ASK 或独立 MID。ASK 未来需要新的 identity/API 设计，将 `price_basis` 纳入主键、分区、selector、cursor、coverage、quality 和 receipt。MID 只能在同窗 BID/ASK 对齐后作为 `synthetic_mid` 或 `approx_mid` 派生；真实 tick midpoint 另立 spec。

## 完成门禁

S1 是 planner、watermark、gap repair、幂等和 contract tests；S2 是核心品种持续维护和正式 acceptance。S2 的完成条件是每个 approved symbol 都有连续 timer 维护证据，所有缺口均有 provider 原始响应或合法 session 的解释，并能产出 ready 区间；不再要求供应商在低流动性分钟人为生成报价。

S1/S2 的 contract test 必须证明 planner、coverage、quality、capacity 和 receipt 逻辑可用于至少一个非 Dukascopy provider；Dukascopy 特有行为只保留在 connector、capability 和 profile。

2026-09-12 生产当前已激活 release `ea41cd80c85f-f616f8c4`（source commit
`ea41cd80c85fe5eaf5e73cde6819e043a51f0c8e`）。此前的 release 验收记录仍保留在
历史 receipt 中。容量策略按 operator 授权调整为
warning 5%、critical 2%；当前约 11.68% free，状态为 `ok`，critical 保护仍保留。
在磁盘为 TB 级且状态为 `ok` 时，容量不作为本阶段 1m/派生工作的阻塞项；仍保留
critical 写保护，不自动删除或改写 canonical 数据。
真实维护证据包括：BTCUSD 2-day 1m BID（2880 行）、EURUSD gap repair（60 行），
以及同一 EURUSD 窗口幂等重跑（`window_count=0`）。随后 EURUSD、GBPUSD、USDCAD、
USDJPY、AUDJPY、GBPJPY、XAUUSD 的固定工作日窗口各补齐 60 行并达到 `ready`。

维护 timer 已启用，生产 env 当前为 8 个 approved symbols 的 allowlist，自动
canary 已成功触发。为应对 Dukascopy 长窗口返回不完整结果，maintenance policy
新增 provider/timeframe 可配置的 `shard_minutes`，Dukascopy 1m 使用 60 分钟分片；
`coverage_not_ready` 在 production/maintenance scope 下可重试，结构性质量错误仍为
终态。此前 release `ea58dc768846-d05c341d` 的部署和重试语义验收已完成；当前
release 的重启 readback 见下方补充记录。

维护 runner 对内部缺口保持 fail-closed：不生成合成分钟，也不把 readiness
伪装成 ready。若缺口位于已观察数据的内部，缺口继续记录在
`unresolved_gaps`；新到达的数据可从 observed watermark 后单独进入 canonical。
同一 terminal gap 受 policy 的 `gap_retry_cooldown_minutes` 控制，避免 timer
每轮重复产生相同 dead-letter。缺口真实补齐后仍须重新通过 coverage/readiness
和下游派生验收。

FX/金属已在不同交易时段完成持续 tail 观察，8 个 approved symbols 均纳入 allowlist。2026-09-12
的生产观察还记录了两类重要结果：BTCUSD 最近分钟窗口出现供应商缺口时，质量门禁
返回 `QualityError/coverage_not_ready` 且没有 canonical publication；容量仍为 `ok`。
对 2026-09-10 12:00--13:00 UTC 的 8 个 approved symbol 固定窗口维护全部通过，
receipt 为 `2026-09-12T023548.252823+0000-e8b9e7629d7f4aa499b68e6e7301ed25.json`。
完整工作日回补因 FX/金属存在真实内部缺口而被拒绝，receipt 为
`2026-09-12T024139.631067+0000-10216f769bb048749df78c0c18c0f908.json`；该失败是
预期的质量保护证据，不得通过填补或放宽门禁解决。
尝试将 allowlist 临时扩大到 8 个 approved symbols 的 timer 轮全部因当前 tail
coverage 不完整而重试/dead-letter，未发布不完整数据；receipt 为
`2026-09-12T034658.205100+0000-b2fe49db145f4a0fafaa49027b5684d2.json`。因此生产
allowlist 已收回 BTCUSD，避免在供应商恢复前制造持续重试负载；该回收是运行策略，
不是放宽质量门禁。

当前版本又完成两轮 BTCUSD timer 幂等观察（04:19、04:35 UTC），均为 `pass`、
`window_count=0`、`readiness_status=ready`；最新 receipt 为
`2026-09-12T043555.446169+0000-f33b95d23d464e35b83a8dec1329d71a.json`。

## 2026-09-12 生产连续观察补充

- 当前 protected-main production release 已前向激活为 `ea41cd80c85f-f616f8c4`，
  source commit 为 `ea41cd80c85fe5eaf5e73cde6819e043a51f0c8e`。API/worker readiness
  为 `ready`，`capacity_status=ok`，free ratio 约 `0.11684`，queue depth 为 `0`。
- timer 已连续完成三轮全 allowlist（8 个 symbol）维护：
  `2026-09-12T121735.702457+0000-dec3362e67a74d8d8f2f279339ec9c28.json`、
  `2026-09-12T123252.313721+0000-3eb4b01322a24f908fd3bc00b6bab4ee.json`、
  `2026-09-12T124841.231957+0000-b0d4d5ba342646458349cc8916be8460.json`。
  每轮均证明 5 个无缺口品种通过、BTCUSD/EURUSD/USDCAD 的真实内部缺口被保留为
  `degraded` 并暴露 ready intervals，由 `gap_retry_cooldown_minutes=180` 抑制重复 repair；没有
  synthetic publication，队列最终归零。
- 通过 `dukascopy_1m_gap_provider_verification` receipt
  `2026-09-12T125054.944451+0000-16600035e0b2412894c34fe9902802b6.json`，使用
  当前 immutable release 的真实 provider 请求复核了上述缺口：EURUSD 缺
  `22:18Z`，USDCAD 缺 `22:04Z/22:14Z/22:23Z`，BTCUSD 缺 `02:21Z`；返回行均为
  BID，缺口来自 provider 原始响应而非 canonical 写入逻辑。
- 完成 API/worker 重启后的生产 readback，receipt 为
  `operations/dukascopy_1m_restart_readback/2026-09-12T124238.958842+0000-bce9f211b5f6440293030edc8ace47ca.json`：
  8 个 symbol 的 API readback 均非空且 BID-only，readiness/heartbeat/queue 通过，
  Dukascopy 346 个受治理 parts 的数量、字节数、SHA-256 和 manifest 集合在重启前后
  完全一致。统一 `bash scripts/ci.sh all` 在该 release source commit 上通过（174
  tests 及 browser/service/secret/operations checks）。
- 以上证据完成了 S2 的运行机制、重启、幂等、容量和真实 provider 缺口保护验收。
  3 个品种的缺口虽无法由供应商补发，但已由原始响应复核并显式标记；因此它们的
  全历史 coverage 为 `degraded`，缺口之外的 ready 区间仍可安全消费。S2 不再把“无
  任何历史缺口”作为门禁，也不允许通过放宽质量规则或生成 synthetic bar 来达标。
- 修订后的 S2 门禁审计 receipt 为
  `operations/dukascopy_1m_s2_gate_audit/2026-09-12T125837.170174+0000-0325e995e02b420b84addc05a902e99e.json`。
  旧审计中的 `all_symbols_ready=false` 仅反映已废止的“全历史零缺口”规则。按
  provider-verified-gap 规则，8 个 symbol 均有可消费 ready 区间，3 个 degraded
  symbol 的维护窗口缺口均有独立 provider verification receipt，S2 正式通过。
- 最终生产 API coverage/readiness 验收 receipt 为
  `operations/dukascopy_1m_s2_provider_verified_gap_acceptance/2026-09-12T134141.022183+0000-25e38caefd9e412eb14f43fbd7a682ca.json`。
  在 deployment `d2fe84443721-6c38e5a3` 上，8 个 approved symbol 全部有物理数据和
  `ready_intervals`；5 个全历史完整，3 个以 `degraded` 显式暴露缺口；API/worker
  readiness、BID-only、queue=0 和 capacity=ok 全部通过。
