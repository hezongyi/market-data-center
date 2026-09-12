# Dukascopy 1m BID Rollout Specification

日期：2026-09-11  
状态：S1 implemented; protected-main production smoke passed; S2 full-universe continuous maintenance pending
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

2026-09-12 已完成一个真实生产 smoke（EURUSD、10 分钟、1m BID）并完成新的
`1m -> 5m` API/worker 回读；当前仍需运行 approved symbol manifest 的 tail/gap
maintenance 观察期，才能关闭 S2。
