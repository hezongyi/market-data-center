# Ingest Contract

一次 ingest 由 `IngestJob` 描述：`job_id`、`dataset_id`、`provider`、selector、date range、
`run_kind`、`run_scope` 和 quality policy。`run_kind` 为
`ingest|derive|backfill|gap_repair|quality|parity`，与
`run_scope=production|acceptance|migration|maintenance` 分开记录且在 run 生命周期内不可变。

执行阶段固定为：queue、fetch、normalize、validate、write、quality、receipt。API 创建 `queued` run；worker 原子领取后写入 `running`，并以 `pass` 或 `failed` 结束同一 `run_id`。失败阶段和错误类型必须写入 run ledger；重复执行不得覆盖已有 receipt。

执行前，平台把 `DatasetDefinition`、provider capability、instrument、session/calendar、
maintenance 和 quality profile 解析为 immutable execution plan。分钟级 backfill、tail 和 gap repair
由通用 window planner 生成有界半开窗口；connector 只负责 provider 请求和标准化。

Raw 和 derived receipt 共享 dataset、schema、run kind/scope、quality、lineage、input/output hash、
manifest 和配置 digest 语义。派生 run 只能读取已发布的 immutable input snapshot。
大批量 run 的 lineage 以 `source_hash_count`、`source_hash_digest`、`first_source_hash` 和
`last_source_hash` 摘要表示，不在 receipt 中展开完整 source hash 列表。
