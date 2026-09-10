# MVP Acceptance

- API 与 Web UI 可独立启动。
- fixture ingest 能生成符合 `provider_bars v1` 的 Parquet。
- run receipt 含 `run_id`、`schema_version`、输入输出 hash、行数和时间范围。
- 质量失败会阻止 run 标记为 `pass`。
- API 可按 symbol、timeframe、时间范围读取 bars。
- Web UI 能查看数据集、run 状态、质量 finding 和数据预览。
- `macro-market-lab` 无需访问 canonical 文件即可通过 API 查询。
- SDK mock 测试验证 API client 会发送 provider、symbol 和 API key。
- `webui` 执行 `npm ci && npm run build` 成功生成 `dist/`。
- Hosted CI 在 Python 3.10/3.11/3.12 与 Node 22 上使用提交的 lock artifacts，并保留结构化 receipt。
- 隔离 Playwright acceptance 覆盖 readiness、dataset list、run filter、失败 retry、未授权写入、bars coverage 与 390px 移动布局。
- 容量 warning/critical 边界、重复告警幂等、backfill/ingest gate 和 read/restore availability 有自动化测试。
- Backup v2 的中断临时产物、原子发布、流式大文件 restore、冲突 fail-closed、v1 compatibility 与跨挂载恢复演练均通过。
