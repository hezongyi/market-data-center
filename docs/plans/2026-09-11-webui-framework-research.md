# Market Data Center Web UI framework research

日期：2026-09-11

范围：界面技术选型，不修改应用代码

## 结论

不建议把一个完整 admin 项目直接覆盖进现有 `webui/`。Market Data Center 不是典型的用户/订单 CRUD 后台：核心界面是服务健康、数据集、运行记录、质量告警、覆盖率、时间序列浏览和带权限的重试/ingest 操作；现有 API 还有统一 envelope、cursor pagination 和命令型端点。完整模板会同时引入它自己的路由、构建器、状态模型、鉴权假设和示例页面，短期看起来快，后续删除模板假设和适配 API 的成本更高。

推荐基线是：

1. **shadcn/ui 的 dashboard/app-shell 作为视觉和布局起点**，在现有 Vite + React + TypeScript 工程内逐个加入组件，不复制整个示例应用。
2. **TanStack Table 负责 Runs、Quality findings 和 Bars 等数据密集表格**；视觉仍由 shadcn/ui/Tailwind 控制。
3. 保留当前 `/api/v1` 调用语义，先抽出 typed API client/query hooks，再迁移页面。暂时不引入 Refine 或 React Admin 的 resource/data-provider 抽象。
4. 若团队更重视开箱即用、统一企业风格而非视觉自主性，第二选择是**直接使用 Ant Design + ProComponents**，不是克隆 Ant Design Pro 整套 Umi 工程。

这个组合最符合“现代、数据密集、运维导向、能渐进迁移”的要求，也不会阻塞正在并行进行的 Dukascopy API 开发。

## 当前工程与约束

当前 `webui/package.json` 只有 React 19、React DOM、TypeScript 和 Vite；`webui/src/main.tsx` 将请求、状态、导航和所有页面集中在单文件。已经存在的业务契约包括：

- `/api/v1` envelope：`data`、`meta.next_cursor`、`errors`；
- cursor-based bars pagination，而不是传统的 page/total；
- `POST /ingest/runs` 与 `POST /runs/{id}/retry` 等命令；
- overview、datasets、runs、quality、explorer 五类视图；
- 浏览器验收由仓库脚本驱动，必须继续使用隔离数据和 API。

因此，界面升级应优先解决 component shell、responsive navigation、数据表格、filtering、loading/error/empty state 和主题 token；不应在第一阶段替换 API 合约或构建系统。

## 候选比较

| 候选 | 官方定位与许可证 | 与本项目的适配度 | 接入现有 Vite/React/TS | 主要代价/风险 | 建议 |
| --- | --- | --- | --- | --- | --- |
| **shadcn/ui** | 官方称其为可复制、修改、扩展的开放代码组件集合，用来构建自己的组件库；MIT。[README](https://github.com/shadcn-ui/ui/blob/main/README.md) · [LICENSE](https://github.com/shadcn-ui/ui/blob/main/LICENSE.md) | 很高。Dashboard、sidebar、card、badge、dialog、sheet、tabs、command、form 等适合监控和数据运维界面，且能建立自己的密度和状态色语义。 | 中低。官方有“Existing Vite Project”流程；需要 Tailwind、`@/*` alias 和初始化配置，之后组件源码落在仓库内。[Vite 安装](https://ui.shadcn.com/docs/installation/vite) · [Dashboard 示例](https://ui.shadcn.com/examples/dashboard) · [Blocks](https://ui.shadcn.com/blocks) | 它不是完整后台框架，也不替项目维护组件；升级不是简单改一个 npm 版本。表格的数据行为仍需 TanStack Table/query 层。 | **首选 UI 基线** |
| **TanStack Table** | Headless datagrid，官方列出 sorting、filtering、grouping、aggregation、row selection，并强调可定制、可虚拟化、server-side friendly；MIT。[README](https://github.com/TanStack/table/blob/main/README.md) · [LICENSE](https://github.com/TanStack/table/blob/main/LICENSE) | 很高。Runs/findings/bars 会迅速超过手写 `<table>` 能舒适承载的范围，headless 模式也不会锁定视觉。 | 低。安装 React adapter 后，以 column definitions 和 controlled state 替换现有表格；和 React 19 兼容性可由 npm peer metadata 核验。[npm metadata](https://registry.npmjs.org/%40tanstack%2Freact-table/latest) | 不提供组件外观、工具栏或 API cache。当前 cursor API 要自定义“下一页/继续加载”状态，不能误建 page-number/total 假设。 | **与 shadcn/ui 组合使用** |
| **Ant Design Pro** | 官方定义为基于 React 的开箱即用企业应用 boilerplate，包含 dashboard、monitor、forms、lists、profile、result/error 等模板；MIT。[README](https://github.com/ant-design/ant-design-pro/blob/master/README.md) · [LICENSE](https://github.com/ant-design/ant-design-pro/blob/master/LICENSE) | 功能覆盖很高，尤其是表格、筛选、状态页和运维 dashboard；视觉成熟，但更像标准企业后台。 | **高（整套 Pro）**。官方工程当前以 Umi Max 构建，不是 Vite；其 package manifest 还包含自己的 scripts、mock、routing/test/build 栈。[package.json](https://github.com/ant-design/ant-design-pro/blob/master/package.json) | 套整项目等于替换构建和应用骨架，容易影响现有 Vite/browser acceptance。模板示例和依赖很多。 | 不套整套；如选 Ant 系，改用 **Ant Design + ProComponents** 渐进接入 |
| **Refine** | 官方称其为面向 CRUD-heavy 企业应用的 headless React meta-framework，覆盖 auth、access control、routing、networking、state、i18n，并能配 Ant Design/MUI 等 UI；MIT。[README](https://github.com/refinedev/refine/blob/main/packages/core/README.md) · [LICENSE](https://github.com/refinedev/refine/blob/main/LICENSE) | 中。Datasets/runs 可以映射为 resources，但 coverage、ingest、retry、health 与 evidence 更偏 command/operations，不是标准 CRUD。 | 中高。可装进 React 工程，但需要实现 custom data provider、router/query 生命周期，并重新安置现有 fetch 逻辑。官方说明 data provider 是其将 CRUD 动作翻译到 API 的核心接口。[Data Provider](https://refine.dev/docs/core/providers/data-provider/) | 对当前五页小应用属于较重的架构承诺。若未来扩展到大量资源、权限和 CRUD 页面，价值会明显上升。 | 当前不引入；作为规模扩大后的复评项 |
| **React Admin** | TypeScript/React 上的 REST/GraphQL SPA framework，基于 Material UI，提供 auth、routing、forms、datagrid、permissions、i18n、cache 等；核心 MIT。[README](https://github.com/marmelab/react-admin/blob/master/README.md) · [LICENSE](https://github.com/marmelab/react-admin/blob/master/LICENSE.md) | 中低。它擅长 record/resource CRUD；本项目更偏时间序列观察和运维命令。 | 中高。需要 DataProvider adapter。官方 contract 要求 record 至少有 `id`，`getList` 使用 page/perPage/sort/filter 并返回 total 或 pageInfo；现有 cursor/envelope 需要转换。[Writing a Data Provider](https://marmelab.com/react-admin/DataProviderWriting.html) | 资源模型和 Material UI 观感会主导应用；commands 和 cursor pagination 需要持续定制。部分高级能力属于商业 Enterprise 组件，采用前要单独核对。[Enterprise Edition](https://marmelab.com/ra-enterprise/) | 不适合作为本次换肤基线 |
| **MUI / MUI X** | Material UI 是完整 React component library；core MIT。MUI X 提供复杂 data components，采用 open-core：MIT community components，高级功能需商业许可证。[Material UI README](https://github.com/mui/material-ui/blob/master/README.md) · [core LICENSE](https://github.com/mui/material-ui/blob/master/LICENSE) · [MUI X licensing](https://mui.com/x/introduction/licensing/) | 高。Data Grid、cards、dialogs、forms、navigation 都成熟，适合内部运维后台。 | 中低。可直接加入现有 Vite 工程；需要 Emotion/Pigment 等 peer dependencies，并建立 theme。React 19 支持可由 npm peer metadata核验。[npm metadata](https://registry.npmjs.org/%40mui%2Fmaterial/latest) | Material 风格辨识度强；一旦需要 MUI X Pro/Premium 的高级 grid 功能，会出现席位/商业许可证决策。 | 稳健备选；优先于完整 admin framework |
| **Tabler** | 官方是基于 Bootstrap 5 的免费开源 HTML dashboard UI kit，含大量 layout/components/demo pages；MIT。[README](https://github.com/tabler/tabler/blob/dev/README.md) · [LICENSE](https://github.com/tabler/tabler/blob/dev/LICENSE) | 视觉和信息架构适合运维后台，成品页很多。 | 中高。官方核心是 HTML/Bootstrap，不是 React component system；接入会在当前 React 工程里增加 Bootstrap CSS/JS 和 wrapper 工作。 | Bootstrap 与若采用的 Tailwind/shadcn 路线冲突；某些交互是 DOM/plugin 思路，长期 React 类型和状态整合弱于原生 React 组件。 | 只作为布局/密度参考，不作为代码基线 |

## 为什么首选 shadcn/ui + TanStack Table

### 1. 可以渐进替换，不需要“大爆炸”迁移

先换 app shell、sidebar、header 和 cards，再逐页替换 runs/findings/bars 表格。旧页面可在迁移中继续工作，现有 Vite proxy、API key、browser acceptance 不必同时重写。

### 2. 视觉现代，但不会把产品变成模板演示站

shadcn/ui 提供的是组件源码和 design tokens，而不是不可拆的整站。可针对市场数据运维调整：更紧凑的表格、等宽标识符、UTC 时间、ready/warning/critical 状态、provider 颜色、危险动作确认、暗色模式。官方 dashboard 示例和 blocks 可用作页面骨架，但应移除销售/收入等业务示例。

### 3. 适合 cursor 和时间序列数据

TanStack Table 的 controlled/headless 模式允许 UI 状态直接映射当前 API：保留 cursor token、snapshot 语义和 server-side filtering，不需要伪造 `total` 或 offset page。对 bars 数据应只展示当前页/窗口；不能继续像现状一样在浏览器里自动遍历所有 cursor 才渲染。

### 4. 许可证简单

shadcn/ui 与 TanStack Table 均为 MIT。复制进仓库的 shadcn/ui 源码仍应保留项目依赖/NOTICE 清单所要求的版权和许可证记录。不要从第三方付费 dashboard、社区截图或无明确许可证的模板复制资产。

## 推荐的信息架构

借用 dashboard 模板的 shell，不借用其业务模型：

- **Overview**：readiness、worker heartbeat、deployment/version/commit、capacity、近 24h runs/失败率、open findings；
- **Data catalog**：dataset/schema/partition/provider/coverage，可搜索并进入详情；
- **Runs**：server-side filter、status tabs、run detail drawer、retry/acknowledge confirmation、error timeline；
- **Quality**：severity/code/dataset/time filters，finding detail 与相关 run；
- **Explorer**：provider/instrument/timeframe/date window，coverage summary、bars table，后续再加 chart；
- **Operations**：ingest/backfill 与高风险操作单独成页，不与只读 explorer 混合；API key 不长期显示在 sidebar。

## 建议的实施次序

1. 建立 `src/lib/api`（envelope/error/cursor）、共享 domain types 和 query hooks；保持端点不变。
2. 在现有 Vite 工程初始化 Tailwind/shadcn，加入 sidebar、button、card、badge、input/select、table、dialog、sheet、skeleton、toast。
3. 把单文件拆成 app shell + routes/pages；先做静态高保真 shell 与 Overview。
4. 用 TanStack Table 迁移 Runs/Quality/Bars，明确 controlled filter 和 cursor navigation，不全量抓取 bars。
5. 为 retry/ingest 增加 confirmation、pending、success/failure、权限不足状态；默认保持只读。
6. 更新隔离 Playwright acceptance，覆盖 desktop/mobile、empty/loading/error、cursor next page 和危险操作确认。

第一阶段不建议加入 Refine、React Admin、Ant Design Pro/Umi 或商业 grid；等页面数、权限矩阵、跨资源 CRUD 明显增长后再复评。

## 维护状态核对

截至 2026-09-11，Refine、Ant Design Pro、Material UI、shadcn/ui、TanStack Table、React Admin 和 Tabler 的 GitHub repositories 均未 archived，且各自默认分支近期仍有提交。此项只是活跃度快照，不替代锁版本、依赖审计与升级测试：

- [Refine repository metadata](https://api.github.com/repos/refinedev/refine)
- [Ant Design Pro repository metadata](https://api.github.com/repos/ant-design/ant-design-pro)
- [Material UI repository metadata](https://api.github.com/repos/mui/material-ui)
- [shadcn/ui repository metadata](https://api.github.com/repos/shadcn-ui/ui)
- [TanStack Table repository metadata](https://api.github.com/repos/TanStack/table)
- [React Admin repository metadata](https://api.github.com/repos/marmelab/react-admin)
- [Tabler repository metadata](https://api.github.com/repos/tabler/tabler)

进入实现前，应让 `npm --prefix webui install` 生成受审查的 lockfile diff，再运行仓库统一 gate；不能依据 `latest` 漂移安装。
