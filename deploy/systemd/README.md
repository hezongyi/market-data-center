# 本地 systemd

生产服务不从仓库 checkout 或仓库内 `.venv` 启动。依赖和 Web UI 由
`data_center.deployment stage` 安装到每个只读 release 的独立 `.venv`：

```bash
git fetch origin main
test -z "$(git status --porcelain)"
PYTHONPATH=backend/src python -m data_center.deployment stage "$(git rev-parse origin/main)" \
  --repository "$PWD" --release-root "$HOME/market-data-center/releases" \
  --evidence-root /home/quant/market_lake/evidence/data-center
```

服务使用 `releases/current/.venv/bin/python`。Yahoo 网络依赖固定为实测可用的
`yfinance==1.7.0` 与 `curl_cffi==0.16.3`。

服务通过 `$HOME/.config/market-data-center/env` 读取 `FRED_API_KEY` 和
`DATACENTER_PROXY_URL`。该文件位于 repository 之外，必须保持权限 `0600`，且不得写入 release。
代理使用 macro-market-lab 的默认地址
`http://192.168.7.33:7890`；`BINANCE_PROXY_URL`、`YFINANCE_PROXY_URL`、
`FRED_PROXY_URL` 可分别覆盖共享地址。macro-market-lab 的 Binance/yfinance
显式默认启用代理，FRED 配置只声明 key；本项目已验证 FRED 也可通过同一代理访问。

将 service 和 timer 文件复制到 `~/.config/systemd/user/` 后，通过 deployment module 激活 release：

```bash
systemctl --user daemon-reload
systemctl --user enable market-data-center-api.service market-data-center-worker.service
systemctl --user enable --now market-data-center-smoke.timer market-data-center-provider-acceptance.timer market-data-center-monitor.timer
PYTHONPATH=backend/src python -m data_center.deployment activate RELEASE_ID \
  --release-root "$HOME/market-data-center/releases" \
  --evidence-root /home/quant/market_lake/evidence/data-center
curl -fsS http://127.0.0.1:18380/api/v1/health/ready
```

API 将 ingest 请求写入 SQLite durable queue；`market-data-center-worker.service` 领取任务并更新同一 `run_id` 的状态。API 与 worker 必须使用相同的 `DATACENTER_CANONICAL_ROOT` 和 `DATACENTER_LEDGER_PATH`。

`market-data-center-monitor.timer` 每分钟运行一次 `data_center.observability`，检查 heartbeat、积压和质量失败并写入幂等 alert outbox；配置 `DATACENTER_ALERT_WEBHOOK_URL` 才会发送外部 webhook。monitor 不改变数据写入结果。

在具备网络、`httpx`、`yfinance` 和 `FRED_API_KEY` 的环境执行生产验收：

```bash
set -a; source "$HOME/.config/market-data-center/env"; set +a
"$HOME/market-data-center/releases/current/.venv/bin/python" -m data_center.acceptance
"$HOME/market-data-center/releases/current/scripts/smoke.sh"
systemctl --user status market-data-center-api.service market-data-center-worker.service market-data-center-provider-acceptance.timer
```

验收通过时，API/worker 写入同一 `run_id`，并保存 Binance `BTCUSDT`、yfinance `SPY` 与 FRED `PAYEMS` 的 receipt。timer 使用每日调度和 acceptance 内置的一小时最小间隔；失败会写入 `alerts.jsonl`，成功或失败 receipt 都保留在 `acceptance-receipts/scheduled/`。
