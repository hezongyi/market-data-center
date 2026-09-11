# ADR: 放弃 raw-only legacy Dukascopy 数据迁移

日期：2026-09-11  
状态：accepted  
范围：`market-data-center` D4 / Dukascopy provider ingest

## 决策

不将 legacy Dukascopy Parquet 中 `price_type=raw` 的数据迁移到 Data Center，也不把它们重标为 `price_type=bid`。

这些文件继续保留在原始存储位置，作为只读 legacy archive。它们不进入 Data Center manifest，不参与 consumer cutover，也不允许通过 migration gate 发布。

## 原因

- inventory 显示真正 legacy 数据的 basis 只有 `raw`，没有可验证的 BID provenance；
- inventory 发现约 214 万重复 timestamp 和约 44.6 万 coverage gaps；
- 当前 canonical 存储 free ratio 约 11.68%，处于 `warning`，禁止 bulk migration；
- 将 `raw` 改标为 BID 会改变数据语义，且不可审计地掩盖不确定性。

## 影响

- 新 Dukascopy connector 继续只生产明确的 BID historical bars；
- D4 的剩余范围是新 BID 数据的 bounded consumer parity、feature flag 切换、观察和回滚；
- legacy raw 文件仍可作为人工回溯输入，但任何重新迁移提议必须新建 migration spec，并先提供独立 provenance、容量、backup/recovery 和 parity 证据；
- 本 ADR 不授权删除、移动、覆盖、压缩或改写 legacy 文件及既有 canonical parts。

## 验证

- 最终 inventory receipt：`3928ef5dac3843b2a4c86ee597f43a10`；
- 585 个真正 legacy 文件、68,331,636 行、5,853,971,477 bytes；basis 仅 `raw`；
- source snapshot 前后相同，未写入 manifest，`bulk_migration_allowed=false`；
- migration planner 对未确认 BID provenance 或容量 warning 的 item 返回显式 `rejected`，不产生 publication side effect。
