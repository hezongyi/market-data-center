# Web UI Modernization Specification

日期：2026-09-11
状态：accepted；Phases 1-4 implemented and verified
相关研究：`docs/plans/2026-09-11-webui-framework-research.md`
API 契约：`docs/api-and-webui-contract.md`

## 目标

将 Market Data Center Web UI 从单文件、基础 CSS 的页面升级为现代的数据运维控制台，同时保留现有 Vite、React、TypeScript 构建链和 `/api/v1` API 契约。

目标用户需要快速回答四类问题：

1. 服务是否可用，当前是否受到容量或 worker 状态影响；
2. 哪些数据集可用，schema 和覆盖范围是什么；
3. ingest、retry、dead-letter 和质量 finding 当前处于什么状态；
4. 如何在保持分页、快照和鉴权语义的前提下检查行情与经济数据。

## 非目标

- 不引入新的后端 API、数据库或 provider；
- 不复制完整的 Ant Design Pro、Refine 或 React Admin 应用骨架；
- 不把 Web UI 变成营销页；
- 不在浏览器中读取 Parquet、SQLite、canonical 或 evidence 路径；
- 不把 cursor API 转换成伪造的 offset/total 分页；
- 不在第一阶段实现完整 RBAC、主题编辑器或复杂图表平台。

## 技术基线

- Vite + React 19 + TypeScript；
- shadcn/ui 风格的源码组件和 design tokens，逐步加入仓库；
- TanStack Table 用于 Runs、Quality 和 Bars 等数据密集表格；
- Lucide 图标，按钮优先使用图标或 icon + label；
- 保持 `npm --prefix webui run build` 和现有隔离 Playwright 入口；
- UI 只通过 typed data-center client 访问 `/api/v1`。

## 模块接口与 seam

页面不得各自实现 `fetch`、envelope 解包、API key 注入或 cursor 循环。建立一个深模块作为前端 API seam：

```text
Page / route
  -> data-center client and query model
  -> /api/v1 envelope, auth, cursor and error semantics
```

该模块负责：

- 统一 `data`、`meta`、`errors` 解包；
- 在请求中注入当前 API key，但不把 key 写入日志或 URL；
- 把 HTTP 错误映射为可展示的安全错误；
- 暴露 readiness、metrics、datasets、runs、findings、bars 和 coverage 查询；
- 暴露 ingest、retry 等命令的 pending/success/failure 状态；
- 对 bars/economic 查询保留 `snapshot_id`、`schema_versions` 和 `next_cursor`；
- 只在用户继续操作时加载下一页，不自动抓取无限数据。

## 信息架构

### Overview

首屏显示 readiness、worker heartbeat、capacity、deployment identity、近 24 小时 runs、失败率和 open findings。容量 `warning` 或 `critical` 必须在顶部状态区可见。

### Data Catalog

按 dataset、schema version、partitioning、provider 和 coverage 浏览数据集；空数据、加载中和读取失败都必须有明确状态。

### Runs

提供 server-side status/filter、时间排序、run detail drawer、失败错误、安全 retry/acknowledge 操作。retry 必须显示确认、进行中、成功和失败四种状态；原始 terminal run 不可被 UI 覆盖。

### Quality

按 severity、code、dataset 和时间筛选 findings，并可关联到 run 或数据集。没有 finding 时显示明确的空状态，而不是空白区域。

### Explorer

按 provider、instrument、timeframe 和日期窗口查询 coverage 与 bars。结果使用受控 cursor 分页；图表属于后续阶段，第一版优先保证表格、时间范围和 schema 元数据正确。

### Operations

容量、备份、恢复、告警和 deployment identity 单独成页。高风险 ingest/backfill 不与只读 Explorer 混在一起。API key 只存在于当前会话状态，不长期展示在侧边栏。

## 视觉系统

- 视觉定位：安静、紧凑、可扫描的运维控制台；
- 深色可折叠侧边栏，浅色内容区；
- neutral/slate 为基础色，teal/blue 为主要操作色；
- `ready/pass` 使用绿色，`warning/queued` 使用琥珀色，`failed/critical` 使用红色；
- 统一 8px spacing 基线，卡片圆角不超过 8px；
- 表格优先保证列对齐、UTC 时间、等宽 run ID 和横向滚动；
- 不使用大面积渐变、装饰性光晕、营销 hero 或无语义彩色卡片；
- 桌面端验收宽度为 1440px，移动端验收宽度为 390px；
- 所有图标按钮必须有 tooltip 或可访问名称，状态不能只由颜色表达；
- loading、empty、error、permission denied 和 capacity protected 都是设计状态，不允许只显示空白。

## 共享 UI 模块

第一阶段建立并复用：

```text
AppShell
Sidebar
TopStatusBar
PageHeader
MetricCard
StatusBadge
FilterBar
DataTable
DetailDrawer
EmptyState
LoadingSkeleton
ErrorState
ConfirmDialog
```

这些模块只承载布局、状态和交互约定，不把 provider 或具体数据集名称硬编码到通用模块中。

## 分阶段实施

### Phase 1：Shell、Overview、Runs

- 建立 design tokens、AppShell、侧边栏和顶部状态栏；
- 抽出 typed API client；
- 迁移 Overview 和 Runs；
- 加入 loading/empty/error/pending 状态；
- 保持现有 API endpoint 和隔离 browser acceptance 可运行。

### Phase 2：Catalog、Quality

- 迁移数据集注册表和质量 findings；
- 引入表格排序、筛选、状态 badge 和 detail drawer；
- 补充桌面与移动端截图验收。

### Phase 3：Explorer

- 迁移 coverage 和 bars 查询；
- 实现显式 cursor next-page；
- 再评估轻量价格/覆盖图表，不引入超出需求的 charting platform。

### Phase 4：Operations

- 展示 capacity、backup、recovery、alerts 和 deployment identity；
- 对 ingest/backfill、retry、acknowledge 增加确认和权限拒绝状态。

## 验收标准

- `npm --prefix webui run build` 通过；
- Playwright 覆盖 1440px 与 390px；
- readiness、datasets、runs filter、retry、unauthorized write、bars coverage 继续通过；
- 新增 loading、empty、API error、capacity warning 和 permission denied 状态；
- 页面不会直接读取文件系统或生产 `.env.local`；
- API client 保留 envelope、request id、schema version、snapshot id 和 cursor 语义；
- 每个可变写操作都有 pending、success、failure 和不可覆盖 terminal run 的验证；
- 视觉评审从信息层级、状态表达、密度与可读性、操作路径、响应式表现五项记录。

## 开发沟通约定

任务开始：

```text
[UI/<area>] <title>
目标：
范围：
不做：
接口来源：
视觉意图：
验收标准：
工作区：
```

开发更新：

```text
[UI/<area>] progress
已完成：
当前截图：desktop / mobile
已验证：build / e2e / endpoint
发现的问题：
需要决策：选项、建议
```

交付评审：

```text
[UI/<area>] ready for review
改动：
视觉检查：1440px / 390px
测试：
已知限制：
建议下一步：
```

视觉反馈必须明确落在：信息层级、状态表达、密度与可读性、操作路径、响应式表现。

## 决策记录

- 2026-09-11：采用 shadcn/ui 风格源码组件 + TanStack Table 的渐进路线；不套完整 admin 工程。
- 2026-09-11：先实现 AppShell、Overview、Runs，再迁移 Catalog、Quality、Explorer 和 Operations。
- 2026-09-11：视觉原型评审选择 A（Calm operations）：浅色内容区、深色侧栏、克制阴影和高可读运维状态；B（Dark data room）与 C（Queue workstation）不作为正式基线。

## 实施与验收记录

2026-09-11 完成 Phases 1-4：

- 建立 typed API client、共享状态组件、TanStack Table 和 Lucide 图标体系；
- 完成 Overview、Data Catalog、Runs、Quality、Explorer、Operations 六个工作区；
- Overview 使用 `production_sli["24h"]` 展示近 24 小时 runs 与失败率；
- Explorer 保留 snapshot/schema/cursor 语义，支持 bars 日期窗口和 economic current/PIT；
- ingest、retry、acknowledge 均有确认、pending、成功和拒绝状态，terminal run 保持不可覆盖；
- Operations 的 Active alerts 从现有 capacity、dead-letter 和 operational snapshot 聚合；当前 API 契约没有 alert-list endpoint，因此未虚构新的后端接口。

验证结果：

- `bash scripts/ci.sh all` 通过，包括 134 个 backend tests、Ruff、依赖与兼容性、secret scan、operations acceptance、容量性能、Web build、隔离 browser acceptance 和 service restart acceptance；
- browser acceptance 覆盖 29 项状态与操作检查，1440px 和 390px 均通过，无页面级横向溢出或 JavaScript error；
- 视觉检查：信息层级清晰；状态同时使用文字和颜色；桌面密度适合扫描；写操作与只读 Explorer 分离；移动端导航为两行三列，Operations 表单与详情为单列，无遮挡。
