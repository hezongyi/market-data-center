# Market Data Center

本地生产数据资产服务，覆盖 Binance、yfinance 与 FRED ingest、immutable Parquet/manifest、运行 ledger、economic current/PIT 查询、worker、告警、容量保护、原子备份恢复和管理 Web UI。

## Runtime

- Python 3.10、3.11、3.12（hosted CI 全矩阵）
- Node.js 22（Web UI 构建与 Playwright browser acceptance）
- Python 依赖由 `backend/constraints/py310.txt`、`py311.txt`、`py312.txt` 固定完整传递解析；`pyproject.toml` 继续声明支持范围。

## Clean Checkout

```bash
python -m venv .venv
lock="backend/constraints/py$(.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")').txt"
.venv/bin/python -m pip install -c "$lock" -e './backend[dev]'
npm --prefix webui ci
npm --prefix webui run build
cp .env.example .env.local
```

启动 API 与 worker：

```bash
set -a; . ./.env.local; set +a
PYTHONPATH=backend/src .venv/bin/python -m data_center.api
PYTHONPATH=backend/src .venv/bin/python -m data_center.worker_main
```

提交 fixture ingest 后可从 `/api/v1/runs/{run_id}`、`/api/v1/bars` 和 Web UI 验证结果。写接口在设置 `DATACENTER_API_KEY` 时要求 `X-API-Key`。

## Verification

统一入口：`bash scripts/ci.sh`。它执行 lock/warning 检查、ruff、pytest、secret/compatibility 检查、operations acceptance、Web UI build、隔离 Playwright acceptance 和 service restart acceptance。

浏览器默认使用 Playwright 管理的 Chromium；本机已有浏览器时可设置 `PLAYWRIGHT_BROWSER_EXECUTABLE=/path/to/chrome`。该变量只属于本机环境，不写入 `.env.example`。

## Operations

- readiness：`/api/v1/health/ready` 分别报告 read availability、write protection 和 capacity status。
- warning free ratio 默认 15%，阻止超过 31 天的无人值守 backfill；critical 默认 10%，拒绝新 ingest，但查询和恢复保持可用。
- backup v2 先写同目录 `.partial`，校验后原子发布；restore 固定缓冲流式复制并拒绝覆盖不同内容。
- release、容量、备份、verify、恢复演练、CI 与浏览器验收均保留结构化 receipt。

发布与回滚步骤见 `docs/release-checklist.md`，日常操作见 `docs/operations-runbook.md`。
