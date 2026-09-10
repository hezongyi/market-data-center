# API 与 Web UI Contract

API 前缀固定为 `/api/v1`，响应包含 `data`、`meta.request_id`、`meta.schema_version` 和 `errors`。查询接口只读；触发 ingest 返回 `run_id`，由 worker 异步执行。

`GET /api/v1/bars` 与 `GET /api/v1/economic/observations` 支持 `page_size` 和 opaque
`cursor`。显式分页默认 1,000 行、最大 10,000 行；响应 `meta` 增加 `count`、
`schema_versions`、`snapshot_id` 和 `next_cursor`。cursor 绑定 selector、时间范围、查询模式、
排序键和 immutable catalog snapshot，consumer 只回传 cursor，不解析或拼接其内容。未传分页参数时
`/api/v1` 暂时保留完整结果行为，超过 1,000 行时在 `meta.warnings` 返回 `unbounded_query`。

`GET /api/v1/metrics` 的稳定字段包括 `runs_total`、`runs_by_status`、`retry_attempts_total`、
`timeouts_total`、`worker_heartbeat_age_seconds`、`queue_depth`、`queue_oldest_age_seconds`、
`dead_letter_total`、`success_rate` 和 `duration_seconds`（含 `count`、`sum`、`max`、`mean`）。
`query` 子对象包含 catalog refresh/cache、query duration、rows scanned/returned 和 rejected oversized
query 计数。
metrics 只返回聚合状态，不返回 provider 原始响应、URL 或密钥。

Web UI 只调用 API，不直接读取 Parquet 或 SQLite。MVP 页面：Overview、Datasets、Runs、Quality、Data Explorer。

经济数据：`POST /api/v1/economic/ingest` 触发受鉴权的 FRED ingest，并写入
`economic_observations.v2`；`GET /api/v1/economic/observations` 只读取 published manifest snapshot，
支持 `provider`、`series_id`、`start`、`end`、`mode=current|pit` 和 PIT 所需的 `asof_ts`。
`meta.schema_versions` 只来自参与 snapshot 的受验证 manifest；兼容字段
`meta.economic_schema_version` 在单版本时返回 `v1`/`v2`，混合时返回 `mixed`，空结果返回
`unknown`。查询接口绝不在请求中直接调用 provider。
