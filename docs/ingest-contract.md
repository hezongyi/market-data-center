# Ingest Contract

一次 ingest 由 `IngestJob` 描述：`job_id`、`dataset_id`、`provider`、selector、date range 和 quality policy。

执行阶段固定为：queue、fetch、normalize、validate、write、quality、receipt。API 创建 `queued` run；worker 原子领取后写入 `running`，并以 `pass` 或 `failed` 结束同一 `run_id`。失败阶段和错误类型必须写入 run ledger；重复执行不得覆盖已有 receipt。
