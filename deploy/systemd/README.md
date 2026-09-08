# 本地 systemd

将 `market-data-center-api.service` 复制到 `~/.config/systemd/user/` 后执行：

```bash
systemctl --user daemon-reload
systemctl --user enable --now market-data-center-api.service market-data-center-worker.service
curl http://127.0.0.1:18380/api/v1/health
```

API 将 ingest 请求写入 SQLite durable queue；`market-data-center-worker.service` 领取任务并更新同一 `run_id` 的状态。API 与 worker 必须使用相同的 `DATACENTER_CANONICAL_ROOT` 和 `DATACENTER_LEDGER_PATH`。
