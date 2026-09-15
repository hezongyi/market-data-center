# Web UI

React + TypeScript + Vite 前端，只依赖版本化 API。当前生产界面仍主要使用旧自制组件/CSS；shadcn-admin 主线改造已批准但尚未实施。

开发先读 [webui/AGENTS.md](AGENTS.md)、[产品/UI 规范](../docs/specs/2026-09-15-eurusd-first-product-baseline.md)、[开发指南](../docs/development-guide.md)及[本地参考来源](../docs/references/shadcn-admin.md)。

普通 `npm run dev` 的代理默认仍指向 18380，不等于隔离预览，也不用于试写。使用仓库根目录的 `bash scripts/dev-preview.sh start --id <preview-id>` 启动独立 API、worker、scheduler 和 Vite；管理入口会显式注入该预览的代理目标、身份和 fixture 模式。
