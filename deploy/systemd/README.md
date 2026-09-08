# 本地 systemd

将 `market-data-center-api.service` 复制到 `~/.config/systemd/user/` 后执行：

```bash
systemctl --user daemon-reload
systemctl --user enable --now market-data-center-api.service market-data-center-worker.service
curl http://127.0.0.1:18380/api/v1/health
```
