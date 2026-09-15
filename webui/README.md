# Web UI

React + TypeScript + Vite 前端，只依赖版本化 API。当前生产界面仍主要使用旧自制组件/CSS；shadcn-admin 主线改造已批准但尚未实施。

开发先读 [webui/AGENTS.md](AGENTS.md)、[产品/UI 规范](../docs/specs/2026-09-15-eurusd-first-product-baseline.md)、[开发指南](../docs/development-guide.md)及[本地参考来源](../docs/references/shadcn-admin.md)。

现有 `npm run dev` 固定代理 API 到 18380，不等于隔离预览；独立四进程预览入口按 P0 交付。在 P0 完成前，不用默认开发代理自由试写生产。
