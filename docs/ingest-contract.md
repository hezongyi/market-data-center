# Ingest Contract

一次 ingest 由 `IngestJob` 描述：`job_id`、`dataset_id`、`provider`、selector、date range 和 quality policy。

执行阶段固定为：plan、fetch、normalize、validate、write、quality、receipt。失败阶段和错误类型必须写入 run ledger；重复执行不得覆盖已有 receipt。

