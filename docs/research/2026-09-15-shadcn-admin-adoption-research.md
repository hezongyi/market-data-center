# shadcn-admin 采用研究（提案）

日期：2026-09-15。状态：研究完成，采用方向已由维护者确认。本文保留源码调查与取舍；正式要求见[产品与 UI 基线](../specs/2026-09-15-eurusd-first-product-baseline.md)，本地参考来源见[引用记录](../references/shadcn-admin.md)。

研究问题：以用户满意的 shadcn-admin 外观和交互为基础，如何把 Dukascopy EURUSD 数据维护做成顺畅的 WebUI？

## 范围与证据基线

- 名称假设：用户指的是 [satnaing/shadcn-admin](https://github.com/satnaing/shadcn-admin)，演示站为 <https://shadcn-admin.netlify.app/>。如果用户指另一个同名项目，需要重新核对。
- 研究取得的上游默认分支源码：`e16c87f213a5ba5e45964e9b67c792105ec74d26`，`package.json` 版本 `2.2.1`；commit 时间 `2026-06-11T17:08:35+07:00`。采集日期不等同于 commit 日期或 GitHub `pushed_at`。
- 本仓库源码基线：`95ea63daf79ea9698e107e5a54e1500c81304dfa`。
- 方法：只读官方 README、LICENSE、依赖表和组件/鉴权源码，核对本仓库调用边界。没有启动上游演示站进行实际视觉、可访问性或性能验收；下面将源码事实与采用建议分开。

## 结论

**可以采用，建议把它作为成套界面基础迁入现有 `webui/`，按 EURUSD 的用户任务逐页替换。** 采用范围应包含 shell、sidebar、theme、表格工具栏、表单、抽屉、弹窗与交互状态，不能再次变成“只取几个基础组件，其余全部手写”的结果。

不建议独立维护第二个前端仓库，也不建议把上游项目整目录覆盖当前前端。当前已有可复用的 API client、业务 services、权限/请求状态模型及真实登录服务；舍弃这些会同时引入接口、部署、鉴权与视觉变更。短期独立预览入口或 worktree 合理，但最终保持一个 `webui/`、一个前端构建产物。

上游提供登录页面和可选 Clerk 集成，**默认登录是 mock，不是拿来即用的本地认证后端**。本项目应把上游登录页面接到现有 `/api/v1/auth/*`。

## 官方项目实际提供什么

| 项目 | 已查事实 | 对采用的影响 |
| --- | --- | --- |
| 定位 | README 称为 dashboard UI collection，并明确写了 “This is not a starter project (template) though.”；包含亮暗主题、响应式、sidebar、command search、10+ 页面。[U1] | 可以复制并改造成项目界面，但不要假定所有演示业务已经工作。 |
| 技术栈 | React 19、Vite、TypeScript、Tailwind 4、Radix、TanStack Router/Query/Table、React Hook Form、Zod。[U2] | 与 MDC 的 React/Vite/TS 方向一致。无需换成 Next.js/Umi；路由、表单和查询依赖可以按实际需要引入。 |
| 动画 | 导入 `tw-animate-css`；dialog 使用淡入缩放约 200ms，sidebar 有 200ms 过渡，sheet 打开/关闭有 500/300ms 滑动。[U3][U4][U5] | 原样保留交互手感作为起点，再由用户预览确定密度、节奏；需要另外验收 reduced-motion。 |
| 普通登录 | `sleep(2000)` 后写 `mock-access-token` 和 mock user。[U6] | 只能取布局/表单，必须接真实认证服务。 |
| 注册、忘密 | 都等待两秒后 toast 显示成功；忘密跳 OTP 页。[U7][U8] | 不应展示尚未实现的注册、邮件找回、OTP、第三方登录按钮。 |
| Clerk | README 写 “Auth (partial)”；`/clerk` 使用 `VITE_CLERK_PUBLISHABLE_KEY`，源码明确集成可选。[U1][U9] | Clerk 是另一个认证系统的前端集成示例。引入它还需配置服务、处理本项目 API 的凭据验证，不是更换登录页即可。 |
| 用户管理 | Clerk 示例有登录检查，但仍从 `features/users/data/users` 导入展示数据。[U10] | 不等于已经带真实本地用户 CRUD、角色权限管理。 |
| Tasks | 演示任务用 Faker，表格用客户端过滤/排序/页码分页。[U11][U12] | 可借外观，不能直接将模板 tasks 当成 MDC 调度器，也不能拿演示分页替代 API cursor。 |
| 许可证 | MIT 允许复制修改分发，要求保留版权与许可声明。[U13] | 记录引入 commit 和复制文件清单，附版权和 LICENSE；自行负责修改后的维护。 |

上游 `_authenticated` 路由名称本身也不是安全证明：该路由仅挂载 `AuthenticatedLayout`，布局负责 sidebar 和 outlet。[U14][U15] 真实数据权限仍由 MDC 后端判定。

## 当前仓库对照

1. `webui/package.json:11` 的实际依赖只有 React、Vite、TypeScript、Lucide 和 TanStack Table 等，**没有安装 shadcn/Radix/Tailwind**。`src/main.tsx:4` 导入手写 `style.css` 与 `modernization.css`；后者 `:6` 还对 shell 下所有元素覆盖 `letter-spacing: 0 !important`。因此目前不满意的外观，不能直接归因于“shadcn/ui 没动画”。
2. `webui/src/main.tsx:21` 使用 state tab 控制导航；没有真实 URL 路由。采用上游 Router 的实际价值是任务详情有可分享地址、刷新后能留在当前任务，以及预览反馈能定位具体页面。
3. `webui/src/lib/api.ts:723` 已集中实现 envelope、错误和带 cookie 的请求；`:749` 已有登录、退出、会话查询、初始化、改密调用。
4. `webui/src/services/index.ts:40` 及 `:110` 已有领域服务和生产任务/调度器 API；`webui/src/hooks/index.ts:14` 明确查询、权限和异步写入状态。新页面优先复用这些，不直接散落 fetch，也不因为上游用了 Query 就一次替换所有现有 hooks。
5. `backend/src/data_center/api/app.py:270` 的登录调用 `AuthStore`，`:275` 下发 HttpOnly / SameSite cookie；`:284` 起有初始化、退出、查询会话和改密端点，`:335` 校验 cookie 写请求的 origin。这是当前真实认证基础，不应换成 JS 写 mock token。
6. `webui/vite.config.ts:6` 把 `/api` 固定代理到 `http://127.0.0.1:18380`。仅启动一个新前端端口不等于隔离预览环境；预览需显式指定独立 API 端口及数据/ledger/worker/scheduler 根目录，并让 cookie 与 origin 配置匹配预览地址。
7. `backend/src/data_center/api/app.py:1213` 当前用 `StaticFiles(..., html=True)` 托管前端；该配置不能直接视为任意 SPA 深层路由的 `index.html` fallback。采用 Router 时需补同源代理/服务端 fallback，并验收直接打开和刷新任务详情 URL；API 和不存在的静态资源不能被误回退成 HTML。

上述为源码判断，不表示这些路径已经在生产跑顺。本次未调用生产写端点。

## 三种接入方式

| 方式 | 评价 | 建议 |
| --- | --- | --- |
| 整个上游项目覆盖 `webui/`，再补 API | 能很快看到模板，但要拆假数据、mock auth、演示页面、升级整个工具链并重新接已有业务。风险集中在一次大提交。 | 不选。 |
| 永久新建 `webui-next/` 或独立仓库 | 样式完全隔离，但维护两个 lockfile、构建、部署和接口适配层；很容易长期追赶旧 UI。 | 不作为最终架构。短时可用于静态预览或隔离 worktree。 |
| 现 `webui/` 内引入上游界面体系，按完整页面替换 | 复用业务边界，每阶段都可运行、预览和回退；需要处理旧全局 CSS 与 Tailwind 的冲突。 | **推荐。** |

推荐的实现方式：先建立新的 shell/主题和 EURUSD 首个页面入口；旧页面暂时通过明确的兼容入口保留。旧 CSS 要限定作用域或通过独立 HTML 入口隔离，不能继续全局叠加覆盖新组件。需要依靠多入口隔离时，仍由同一个 Vite 工程构建；核心 EURUSD 页面迁完后移除临时入口。是否采用多入口由首个可视切片的 CSS 验证结果决定，不预先建设完整迁移平台。

上游当前依赖比本仓库新，包括 Vite 8/TypeScript 6 等。[U2] **采用 UI 不要求照搬整个 package.json**；先确定复制组件的最小依赖和 peer 要求，锁定版本，保持本仓库 npm/lockfile 工作方式。确实需要升级构建工具时，拆成可独立验证的变更。

## 建议交付切片与用户验收

每片都提供运行中的预览 URL、对应 commit、可操作步骤和已知缺口。前两片开始就邀请用户验收，不需要等完整功能完成或生产 tag。

1. **界面基线。** 上游 shell、主题、sidebar、真实登录页面；EURUSD 任务列表和详情结构。用明确标识的预览数据快速确认外观、表格密度、抽屉和交互。用户先确认“确实是喜欢的 shadcn-admin 风格”。
2. **真实只读链路。** 列表/详情接现有 API，显示覆盖范围、最近一次执行、下次计划时间和阻塞原因；URL 可直达任务，空/加载/错误/未登录状态完整。预览由隔离后端支持。
3. **新建与手动执行。** 按 provider/instrument 能力选择 Dukascopy EURUSD，配置源数据、派生周期、历史起点、调度；预览影响后保存/执行；区分“提交排队”和“最终成功”。用户操作完整创建流程。
4. **维护与恢复。** 暂停/恢复、改计划、重试失败执行、查执行步骤和数据输出；页面可看到源数据与派生数据是否满足任务，不以一条绿色 toast 作为完成。
5. **一个可复用的闭环。** 用户自行新建第二个配置允许的维护任务，能管理并通过 API 查结果。其余 provider/复杂治理页面按后续优先级逐步迁移。

这些是切片建议，真实字段和验收结果必须以收敛后的 EURUSD 产品范围为准，不能反过来受模板 demo 的字段支配。

## 轻量规范建议

建议用一份短的 `webui/AGENTS.md` 或 `docs/webui-conventions.md` 配合可运行示例，不再为每个按钮单独写大 spec。以下内容目前只是提案，并未修改仓库指令。

- **视觉来源：** 记录固定上游 commit；shell、间距、主题、表格/表单/抽屉样式以经用户确认的基线页为准。改动要保持同一套设计语言。
- **组件边界：** `components/ui` 存复制的基础组件，`components/layout` 存 shell，`features/*` 实现数据中心用户任务；API 仍走 `lib/api` 和 services。不要在 JSX 内混入临时 HTTP 请求。
- **业务交互：** 表单选择来自能力 API；列表保留服务端 cursor 语义；刷新不得丢用户输入。写操作区分验证、排队、运行、最终成功、降级/失败。无证据时不显示“已完成”。
- **认证：** 接现有 cookie session；路由状态仅负责体验，写权限由后端决定；默认不引入注册、邮件找回、Clerk 或复杂 RBAC。
- **可用性：** 空、慢、错误、权限不足状态作为页面交付的一部分；保留 focus、键盘/Escape、mobile drawer、减少动画偏好。保留现有语言与时区需求，中文不能仅机械替换英文造成溢出。
- **更新：** 保存复制清单、LICENSE、上游 commit；按需挑上游修复。README 明确若用 shadcn CLI 覆盖定制组件，需人工合并相关修改。[U1] 不建立定期追平整个上游的耗时义务。
- **验收：** 每个可见切片有固定预览入口和简短操作路径；视觉调整跑 build 与相关浏览器检查，调度/鉴权/数据语义变更增加相应行为验证。产品体验验收提前到预览阶段；生产门禁不能替代产品验收。

与 `2026-09-11-webui-framework-research.md` 的关系：保留其中 API/cursor 适配和避免整套覆盖的判断；更新视觉来源优先级，以用户明确喜欢的 **shadcn-admin 成套页面与交互** 为目标。旧研究是历史建议，不能用于否决用户现在的偏好。

## 固定版本官方来源

以下链接均指向此次取得的上游 commit，避免默认分支后续漂移。

- [U1 — README：定位、特性、定制组件、Auth partial](https://github.com/satnaing/shadcn-admin/blob/e16c87f213a5ba5e45964e9b67c792105ec74d26/README.md#L9-L75)
- [U2 — package.json：依赖和版本](https://github.com/satnaing/shadcn-admin/blob/e16c87f213a5ba5e45964e9b67c792105ec74d26/package.json#L21-L93)
- [U3 — 动画 CSS 引入](https://github.com/satnaing/shadcn-admin/blob/e16c87f213a5ba5e45964e9b67c792105ec74d26/src/styles/index.css#L1-L3)
- [U4 — Dialog 动画](https://github.com/satnaing/shadcn-admin/blob/e16c87f213a5ba5e45964e9b67c792105ec74d26/src/components/ui/dialog.tsx#L38-L62)
- [U5 — Sheet 动画](https://github.com/satnaing/shadcn-admin/blob/e16c87f213a5ba5e45964e9b67c792105ec74d26/src/components/ui/sheet.tsx#L55-L66)；[Sidebar 过渡](https://github.com/satnaing/shadcn-admin/blob/e16c87f213a5ba5e45964e9b67c792105ec74d26/src/components/ui/sidebar.tsx#L218-L229)
- [U6 — 普通登录 mock](https://github.com/satnaing/shadcn-admin/blob/e16c87f213a5ba5e45964e9b67c792105ec74d26/src/features/auth/sign-in/components/user-auth-form.tsx#L54-L82)
- [U7 — 注册演示](https://github.com/satnaing/shadcn-admin/blob/e16c87f213a5ba5e45964e9b67c792105ec74d26/src/features/auth/sign-up/components/sign-up-form.tsx#L53-L64)
- [U8 — 忘密演示](https://github.com/satnaing/shadcn-admin/blob/e16c87f213a5ba5e45964e9b67c792105ec74d26/src/features/auth/forgot-password/components/forgot-password-form.tsx#L38-L51)
- [U9 — Clerk 配置与可选集成说明](https://github.com/satnaing/shadcn-admin/blob/e16c87f213a5ba5e45964e9b67c792105ec74d26/src/routes/clerk/route.tsx#L17-L35)；[移除 Clerk 的官方说明](https://github.com/satnaing/shadcn-admin/blob/e16c87f213a5ba5e45964e9b67c792105ec74d26/src/routes/clerk/route.tsx#L110-L127)
- [U10 — Clerk user management 仍使用示例数据](https://github.com/satnaing/shadcn-admin/blob/e16c87f213a5ba5e45964e9b67c792105ec74d26/src/routes/clerk/_authenticated/user-management.tsx#L19-L45)
- [U11 — Tasks 示例数据](https://github.com/satnaing/shadcn-admin/blob/e16c87f213a5ba5e45964e9b67c792105ec74d26/src/features/tasks/data/tasks.ts#L1-L27)
- [U12 — Tasks 客户端分页](https://github.com/satnaing/shadcn-admin/blob/e16c87f213a5ba5e45964e9b67c792105ec74d26/src/features/tasks/components/tasks-table.tsx#L69-L100)
- [U13 — MIT LICENSE](https://github.com/satnaing/shadcn-admin/blob/e16c87f213a5ba5e45964e9b67c792105ec74d26/LICENSE#L1-L21)
- [U14 — 默认 authenticated route](https://github.com/satnaing/shadcn-admin/blob/e16c87f213a5ba5e45964e9b67c792105ec74d26/src/routes/_authenticated/route.tsx#L1-L6)
- [U15 — AuthenticatedLayout](https://github.com/satnaing/shadcn-admin/blob/e16c87f213a5ba5e45964e9b67c792105ec74d26/src/components/layout/authenticated-layout.tsx#L14-L40)
