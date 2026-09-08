# API 与 Web UI Contract

API 前缀固定为 `/api/v1`，响应包含 `data`、`meta.request_id`、`meta.schema_version` 和 `errors`。查询接口只读；触发 ingest 返回 `run_id`，由 worker 异步执行。

Web UI 只调用 API，不直接读取 Parquet 或 SQLite。MVP 页面：Overview、Datasets、Runs、Quality、Data Explorer。

经济数据：`POST /api/v1/economic/ingest` 触发受鉴权的 FRED ingest；`GET /api/v1/economic/observations` 只读取 canonical Parquet，支持 `provider`、`series_id`、`start` 和 `end`。查询接口绝不在请求中直接调用 provider。
