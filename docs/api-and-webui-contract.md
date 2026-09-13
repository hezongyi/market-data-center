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
`capacity` 子对象包含 total/used/free bytes、free ratio、warning/critical thresholds 和 `ok|warning|critical`；稳定字段还包括 `last_successful_backup_at`、`last_successful_recovery_drill_at` 与 `temporary_backup_count`。
metrics 只返回聚合状态，不返回 provider 原始响应、URL 或密钥。

`GET /api/v1/health/ready` 同时返回 `read_status`、`write_status` 和 `capacity_status`。容量 critical 只保护写路径，不把可读取的服务误报为完全不可用。

Web UI 只调用 API，不直接读取 Parquet 或 SQLite。工作区：Overview、Data catalog、Maintenance、
Runs、Quality、Explorer 和 Operations。

经济数据：`POST /api/v1/economic/ingest` 触发受鉴权的 FRED ingest，并写入
`economic_observations.v2`；`GET /api/v1/economic/observations` 只读取 published manifest snapshot，
支持 `provider`、`series_id`、`start`、`end`、`mode=current|pit` 和 PIT 所需的 `asof_ts`。
`meta.schema_versions` 只来自参与 snapshot 的受验证 manifest；兼容字段
`meta.economic_schema_version` 在单版本时返回 `v1`/`v2`，混合时返回 `mixed`，空结果返回
`unknown`。查询接口绝不在请求中直接调用 provider。

## v0.4 数据维护工作台 contract

`POST /api/v1/maintenance/plans` 是无副作用的校验预览：请求体为 maintenance task
（`run_kind`、`run_scope`、`dataset_id`、selector、时间范围、recipe/price basis），响应 `data`
包含 `task`、`validation`（`status` 与带 `field`/`code` 的 `errors`）、`plan`（`plan_id`、
`window_count`、半开区间 windows）、`coverage`、`snapshot`、`recipe`、`capability` 和 `capacity`
（含 `write_status` 与 `protected_reason`）。预览不排队任何运行。

`POST /api/v1/maintenance/tasks` 是唯一写入口，返回统一的 queued envelope：`status=queued`、
`task_id`、`run_kind`、`run_scope`、`dataset_id`、`run_id`/`run_ids`、`window_count`、`plan_id`、
`input_snapshot_id`、`capacity`、`submitted_at` 和 `audit_id`。`run_kind` 取
`ingest|backfill|gap_repair|derive|quality|parity`；`quality` 与 `parity` 是只读校验运行，只记录
findings，不发布 canonical part。`/derive/runs`、`/economic/ingest` 与 `/quality/checks` 复用同一
contract（后者由同步返回改为 202 queued）。容量 critical（或 warning 下超过 31 天的 backfill）返回
507 `capacity_protected`，鉴权失败返回 401，校验失败返回 422 并带稳定 `code`；被拒绝的提交同样进入
写审计。

`GET /api/v1/runs` 支持 `status`、`dataset_id`、`run_kind`、`run_scope`、`provider`、`symbol`、
`created_from`、`created_to`、`page_size` 与 opaque `cursor`。cursor 绑定完整 filter 集合与排序键
（`created_at desc, run_id desc`），filter 变化后复用旧 cursor 返回 422 `cursor_error`；不传分页参数
时保留完整结果行为并在超过 1,000 行时返回 `unbounded_query` 警告。`GET /api/v1/runs/{run_id}` 仍是
不变的存储 receipt；`GET /api/v1/runs/{run_id}/detail` 返回工作台投影：`stage`、`outcome`、
`terminal`、`degraded_reasons`、`selector`、`time_range`、`window_count`、`input_snapshot_id`、
`manifest_status`、`finding_count`、`retry_chain` 和 `findings`，投影不写回 ledger。

`GET /api/v1/quality/findings` 支持 `severity`、`code`、`dataset_id`、`run_id`、`state`、
`series_id`、`observed_from`、`observed_to`、`page_size` 与 `cursor`，并在 `meta.state_counts` 返回
处理状态计数。finding 具有稳定 `finding_id`、`occurrence_count`、`first_observed_at`、
`last_observed_at` 与 `last_run_id`；重复观测不新建身份也不改写已发布 run。
`POST /api/v1/quality/findings/{finding_id}/state` 记录 `open|acknowledged|resolved`（可选
`resolved_by_run_id`），处理状态与运行结果严格分离。

`GET /api/v1/capabilities` 返回 datasets（含 `kind`）、provider capabilities（asset class、timeframe、
maintenance timeframe、price basis、session profile、已批准 instruments）、recipes、run kind 可作用的
dataset、维护策略与 `write_status`，供表单校验和禁用不可用选项；平台没有提供的 capability 不会被推断。

`GET /api/v1/market-bars/coverage` 在派生行情物理覆盖之外返回 recipe 语义
（source/target timeframe、partial bucket policy、price basis、materialization）与
`input_snapshot_ids`；传入 `start`/`end` 时再返回 readiness、ready intervals 与 gap count。

运维只读视图：`GET /api/v1/operations/queue`（队列深度与 `runs_by_status`）、
`/operations/worker`（heartbeat 状态、in-flight jobs）、`/operations/capacity-history`（live 采样 +
monitor 实际记录的容量迁移事件）、`/operations/receipts`（backup、restore、recovery drill、release、
deployment receipt）以及 `/operations/audit`（写操作审计：actor 指纹、时间、selector、任务类型、结果；
绝不保存凭据）。未被记录的历史不会被插值或补造。
