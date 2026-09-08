# Market Data Center

独立的数据资产服务原型。第一阶段只覆盖 `provider_bars` 的 fixture ingest、质量检查、运行审计、REST API 和管理 Web UI。

## 当前阶段

Phase 0 只冻结边界与接口，不接入真实 provider。

## 目录

- `docs/`：设计 contract、MVP 计划和验收标准
- `backend/`：后端 package 预留目录
- `webui/`：React/Vite 前端预留目录
- `deploy/`：systemd/Docker 部署预留目录

## 本地启动

后端：`cd backend && PYTHONPATH=src python -m data_center.api`

前端：`cd webui && npm install && npm run dev`
