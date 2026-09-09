# API 与 Web UI Contract

API 前缀固定为 `/api/v1`，响应包含 `data`、`meta.request_id`、`meta.schema_version` 和 `errors`。查询接口只读；触发 ingest 返回 `run_id`，由 worker 异步执行。

`GET /api/v1/metrics` 的稳定字段包括 `runs_total`、`runs_by_status`、`retry_attempts_total`、
`timeouts_total`、`worker_heartbeat_age_seconds`、`queue_depth`、`queue_oldest_age_seconds`、
`dead_letter_total`、`success_rate` 和 `duration_seconds`（含 `count`、`sum`、`max`、`mean`）。
metrics 只返回聚合状态，不返回 provider 原始响应、URL 或密钥。

Web UI 只调用 API，不直接读取 Parquet 或 SQLite。MVP 页面：Overview、Datasets、Runs、Quality、Data Explorer。

经济数据：`POST /api/v1/economic/ingest` 触发受鉴权的 FRED ingest；`GET /api/v1/economic/observations` 只读取 canonical Parquet，支持 `provider`、`series_id`、`start` 和 `end`。查询接口绝不在请求中直接调用 provider。
