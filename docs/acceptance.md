# MVP Acceptance

- API 与 Web UI 可独立启动。
- fixture ingest 能生成符合 `provider_bars v1` 的 Parquet。
- run receipt 含 `run_id`、`schema_version`、输入输出 hash、行数和时间范围。
- 质量失败会阻止 run 标记为 `pass`。
- API 可按 symbol、timeframe、时间范围读取 bars。
- Web UI 能查看数据集、run 状态、质量 finding 和数据预览。
- `macro-market-lab` 无需访问 canonical 文件即可通过 API 查询。

