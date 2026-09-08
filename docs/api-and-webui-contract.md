# API 与 Web UI Contract

API 前缀固定为 `/api/v1`，响应包含 `data`、`meta.request_id`、`meta.schema_version` 和 `errors`。查询接口只读；触发 ingest 返回 `run_id`，由 worker 异步执行。

Web UI 只调用 API，不直接读取 Parquet 或 SQLite。MVP 页面：Overview、Datasets、Runs、Quality、Data Explorer。

