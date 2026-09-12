# Dukascopy 1m BID Rollout Specification

日期：2026-09-11  
状态：S1 implemented; protected-main production smoke passed; S2 bounded multi-symbol evidence active; full-universe continuous maintenance pending
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

## 品种与 rollout

先建立 approved symbol/capability manifest，优先覆盖 FX、commodity、crypto 核心品种。每个品种需要固定窗口的 coverage、gap、duplicate、OHLC/hash、重启 readback 和容量证据。legacy `price_type=raw` 文件没有可验证 BID provenance，不自动迁移或重标。

## ASK/MID 非目标

第一阶段不采集 ASK 或独立 MID。ASK 未来需要新的 identity/API 设计，将 `price_basis` 纳入主键、分区、selector、cursor、coverage、quality 和 receipt。MID 只能在同窗 BID/ASK 对齐后作为 `synthetic_mid` 或 `approx_mid` 派生；真实 tick midpoint 另立 spec。

## 完成门禁

S1 是 planner、watermark、gap repair、幂等和 contract tests；S2 是核心品种持续维护和正式 acceptance。S1/S2 完成前，不得声称 Dukascopy 已具备全品种 1m 持续维护能力，也不得关闭旧 consumer maintenance。

S1/S2 的 contract test 必须证明 planner、coverage、quality、capacity 和 receipt 逻辑可用于至少一个非 Dukascopy provider；Dukascopy 特有行为只保留在 connector、capability 和 profile。

2026-09-12 生产已激活 release `77979f3588b1-a626e764`（source commit
`77979f3588b1f93f4828401353759deee5ca2bf9`）。容量策略按 operator 授权调整为
warning 5%、critical 2%；当前约 11.68% free，状态为 `ok`，critical 保护仍保留。
在磁盘为 TB 级且状态为 `ok` 时，容量不作为本阶段 1m/派生工作的阻塞项；仍保留
critical 写保护，不自动删除或改写 canonical 数据。
真实维护证据包括：BTCUSD 2-day 1m BID（2880 行）、EURUSD gap repair（60 行），
以及同一 EURUSD 窗口幂等重跑（`window_count=0`）。随后 EURUSD、GBPUSD、USDCAD、
USDJPY、AUDJPY、GBPJPY、XAUUSD 的固定工作日窗口各补齐 60 行并达到 `ready`。

维护 timer 已启用，生产 env 当前保持 `DATACENTER_MAINTENANCE_SYMBOLS=BTCUSD`，自动
canary 已成功触发。为应对 Dukascopy 长窗口返回不完整结果，maintenance policy
新增 provider/timeframe 可配置的 `shard_minutes`，Dukascopy 1m 使用 60 分钟分片；
`coverage_not_ready` 在 production/maintenance scope 下可重试，结构性质量错误仍为
终态。release `ea58dc768846-d05c341d` 已完成部署和重试语义验收。

FX/金属仍需在不同交易时段完成持续 tail 观察后再扩大 allowlist，S2 尚未宣称全品种完成。2026-09-12
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
