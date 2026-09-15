# EURUSD 优先闭环、shadcn-admin 与渐进验收方案

日期：2026-09-15
状态：研究归档；方向已由维护者于 2026-09-15 确认。下文保留讨论时的分析，正式要求以[EURUSD 首个闭环规范](../specs/2026-09-15-eurusd-first-product-baseline.md)、[预览环境规范](../specs/2026-09-15-isolated-preview-environment.md)和[开发指南](../development-guide.md)为准。
分析基线：`market-data-center-latest`，`main@95ea63daf79ea9698e107e5a54e1500c81304dfa`。

## 1. 建议与判断

可以暂停以 macro-market-lab 迁移为主线的开发，改为独立交付一个产品目标：用户在 WebUI 新建 Dukascopy EURUSD 维护任务，获取 BID 1m 原始数据、派生 5m 数据、自动持续更新、处理缺口，并能通过 WebUI 和 HTTP 查询、管理这些结果。先证明这个闭环，再扩周期、品种和 provider。

现有后端值得保留；主要工作是收窄验收范围、补齐实际用户链路、改善界面与反馈机制，不是推翻存储与调度重写。建议在现有 `webui` 中采用 shadcn-admin 的成套布局、主题和交互，逐页替换业务界面；沿用现有数据中心后端和认证。

开发交付改为：短任务说明 → 可操作预览 → 小 PR 与阶段验收 → main 集成环境 → 功能整体验收 → tag → 生产激活。人类首次看到功能的时间应在小 PR 合并前，甚至在 Draft PR 尚未完成时。

本报告与[shadcn-admin 官方源码研究](2026-09-15-shadcn-admin-adoption-research.md)共同构成可讨论的方案。这里没有宣称旧接管规范已经被废止，也没有把未完成的迁移验收改成通过。

## 2. 仓库证据：问题具体在哪里

| 发现 | 证据 | 对交付的影响 |
| --- | --- | --- |
| 数据中心核心可以独立运行 | `backend/pyproject.toml` 无 macro-market-lab 依赖；`connectors/dukascopy.py` 直接使用 dukascopy-python；`production_tasks.py`、`scheduler.py`、`transform.py` 使用本仓库模块 | 脱钩主要是目标与验收解耦，不需要先拆一个新项目 |
| 跨仓库耦合集中在消费验证和接管工具 | `consumer_acceptance.py:53`、`scripts/adapter_parity.py:19`、`takeover.py`，以及 `docs/integration/macro-market-lab.md` | 可以把迁移核验单独列为 integration 工作，不作为每次产品改动的默认门禁 |
| 旧 spec 的成功条件明确要求旧路径对照 | `2026-09-11-market-bars-derivation-and-macro-cutover.md:41`；接管补充规范 `2026-09-15-scheduler-takeover-remediation-and-acceptance.md:71` 要求新的连续 ≥4h legacy 影子对照，且还包含 crypto 验收 | 一个 EURUSD 功能被更大的迁移和扩面目标拖住 |
| 当前 CI 并没有默认执行真实跨仓库长时对照 | `scripts/ci.sh` 仅有 all/backend/web；其执行列表不含 adapter_parity/consumer_acceptance | 不能把“每次等待数小时”归因于 pytest 或全部 CI；耗时约束主要来自接管 spec 与操作安排，未测量每次实际耗时 |
| 阶段拆分偏技术层次 | `docs/plans/2026-09-14-production-task-scheduler.md:25`：S0 基座、S1 注册表、S2 时间、S3 派发、S4 依赖、S5 完整浏览器闭环与接管 | S1 虽有页面，完整操作反馈仍靠后。应以用户能完成的动作组织切片 |
| 浏览器测试不等于调度生产闭环测试 | `scripts/browser_acceptance.cjs:145` 只启动 API/worker；`:651` 起验证计划操作，`:755` 起 run-now 只等 current_execution 出现，没有启动 scheduler 并等待自动 raw→derived→readback 完成 | “按钮能点击、任务进队列”可以通过，但不能证明“任务会自动跑完” |
| 开发预览未形成隔离的交付契约 | `webui/vite.config.ts:6` 固定代理到 18380；`settings.py:16` 默认指向主机市场数据目录；`webui/README.md` 仍称“预留目录” | 只执行 npm run dev 可能连到现有服务。需要可复现的完整预览栈与清楚的访问说明 |
| 有沟通要求，但缺少可操作的预览交付要求 | `2026-09-11-webui-modernization.md:158` 已有截图/进度/ready-for-review 模板；`:182` 没有固定 URL、数据环境和保留周期要求 | 不能说仓库完全没考虑反馈；现规则没有把“用户现在可操作”落实为交付物 |
| 审批分类过宽且验证重复 | `AGENTS.md:60`、`docs/agent-collaboration.md:109`：任何“生产行为”变化都归入大 PR；PR 模板要求本地 all 和 hosted verify；CI 同时监听 push/pull_request | 普通功能也可能被解释成大 PR；本地与 hosted 重复全量，分支 push/PR 也可能重复执行 |
| 当前前端不是已经采用了 shadcn/ui | `webui/package.json` 没有 Tailwind/Radix/shadcn 相关依赖；`components/ui.tsx` 是自制控件 | 现有观感不能归因于 shadcn/ui 天生没有动画；要明确换成用户喜欢的模板设计体系 |

### 2.1 现场只读快照与事实边界

2026-09-15 04:23–04:25 UTC，通过本机 API 只读核对：

- `/health/ready`：ready，版本 `0.6.2`，commit `95ea63d…`，deployment `95ea63daf79e-17fd1d8f`。
- `/operations/scheduler`：账本 `dispatch_enabled=true`，但 scheduler 上报 `instance_dispatch_enabled=false`；结合 `scheduler.py:386` 的 shadow 分支，此进程不会真实派发。队列 queued=0/running=0。
- `/production/tasks`：9 条计划，EURUSD enabled，其余 8 条 paused；EURUSD 为 catching_up/lagging，阻塞原因为 provider_gap。EURUSD 的 `bar_timeframes=[]`，即当前计划本身根本没有包含派生输出。
- `docs/current-state.md` 仍记录 02:06 UTC 的 v0.6.1/全局暂停；验收索引仍写候选待发布。它们是旧快照，不能作为当前现场状态。

这些证据不等于生产不可用：API 查询可用。它们说明“服务可访问”“计划启用”“调度进程真实派发”“原始与派生数据持续更新”必须分别展示和验收。本轮没有运行真实拉取、启停计划或修改生产配置，也没有声称重新验收了线上全部功能。

### 2.2 本轮验证

执行以下隔离回归，结果 `49 passed in 18.41s`：

```bash
PYTHONPATH=backend/src .venv/bin/python -m pytest -q \
  backend/tests/test_dukascopy_connector.py \
  backend/tests/test_production_dispatch.py \
  backend/tests/test_production_window_planning.py
```

测试包括真实 worker 配合受控 provider、raw 后派生、缺口隔离、暂停恢复、重算等。这支持“已有实现可以复用”，不证明真实 Dukascopy 的持续生产、UI 美观或人类满意度。当前没有必要为了这份分析再跑一次发布级全套门禁。

## 3. 把第一个功能定义为可验收的用户旅程

### 3.1 范围

首个切片固定 Dukascopy、EURUSD、BID、原始 1m、派生 5m；最近一个完整交易日用于首次有界验收，窗口选取须避开尚未可用的 provider 尾部。历史起点由任务配置，但首轮不以全历史回补完成为门槛。通过后，按相同流程增加已有 15m/1h 等 recipe，而不把全部历史和全部周期同时绑进首次交付。

“新增其他数据任务”分清两种操作：

- 为已支持的品种新增任务：首轮收口用 GBPUSD 作为第二个任务，检验非 EURUSD 写死，用户可自行配置、启停和查询。
- 新增尚未注册的品种或全新 provider：当前 instrument 在 `platform_registry.py:73` 起由代码注册，不能声称在页面输入任意代码即可支持。品种元数据管理与 connector 扩展列为后续独立能力；不能猜交易时段、币种或价格口径。

既有 Binance/yfinance/FRED 能力维持兼容，但不继续扩展；economic/PIT 迁移、crypto 接管、ASK/MID、周/月线、全量历史和通用工作流平台退出本轮完成条件。

### 3.2 一次完整验收应让用户完成什么

| 用户动作 | 可观察结果与关键正确性 |
| --- | --- |
| 新建任务 | 选择品种、历史起点、输出周期与频率；预览后创建，校验错误定位到字段；明确初始启停状态 |
| 立即运行 | 显示本轮计划窗口、真实阶段与运行记录；raw 发布后可查询，不以 queued 代替成功 |
| 查看派生 | 同一任务详情直接看到 1m/5m 数据、覆盖与来源；5m 来源于固定 1m 输入，OHLCV 聚合符合规则 |
| 开启自动维护 | 实际 scheduler 到期触发，至少连续两轮自动执行并读回；没有新数据时明确显示“无新增/等待源数据”，不伪造进度 |
| 暂停与继续 | 显示暂停影响、在途状态、恢复后下一轮；重启 API/worker/scheduler 后计划与进度仍可恢复 |
| 处理缺口 | 区分休市、源延迟、真实缺口、网络失败和结构错误；显示受影响时间段与下次复查；缺口不反复耗尽短重试或阻塞其它完整桶 |
| 使用 HTTP | capabilities、任务增查改/动作、运行详情、provider-bars、market-bars、coverage 都有可复制例子；查询与 UI 一致，保留 cursor/snapshot/recipe 语义 |
| 新增第二任务 | 用户通过页面创建 GBPUSD，独立启停；一个品种缺口不阻塞另一个，重复所有权有清晰提示 |
| 归档或删除计划 | 确认框解释删除对象；保留已发布数据与运行历史；失败后草稿和上下文不丢失 |

“完整”指可操作、可查询、可持续、异常可理解与可恢复，不指 provider 从此没有缺失数据。

### 3.3 不依赖旧仓库的正确性依据

保留三层证据：

1. 快速确定性检查：已知 BID 1m 小样本、独立写出的预期 OHLCV、半开时间窗口、重复/乱序、周末/休市/DST、缺口只影响相关桶、幂等与进程重启。不用同一聚合实现生成自己的 expected。
2. 完整隔离链路：WebUI + API + scheduler + worker + 临时 ledger/canonical/auth/evidence。受控 provider 返回正常/缺口/恢复样本；真实走定时派发和发布读回。注入时钟覆盖冷却、停机与 DST，不等待真实 3 小时。
3. 独立真实源验收：直接 Dukascopy、小窗口、有限请求、独立沙箱数据目录；记录源请求范围、BID 口径、行数和 HTTP 读回。首发做至少两轮实际自动调度，观察长度按数据可用性决定，不把 legacy 对照作为通过依据。

当前 Dukascopy 策略 `closed_bar_lag_minutes=180`、`gap_retry_cooldown_minutes=180`（`platform_registry.py:45`）。这不等于 provider 官方承诺 3 小时延迟。UI 必须分开“最近运行”“最新 bar”“按当前策略应到达的 bar”，不能承诺实时分钟数据。后续若调整策略，应依据源观测单独验证。

不应机械删除所有名为 parity 的测试：`quality/verification.py:110` 是本项目内源/派生覆盖与 lineage 校验，并非 macro-market-lab 对照；且它当前也不是逐 OHLCV 数值比对。保留这种内部检查，并补独立聚合预期。跨仓库 parity 仅在明确开展消费者迁移时执行。

### 3.4 与旧迁移规范如何脱钩

在采纳本方案的范围调整 PR 中，逐条登记旧 AC：保留、被新验收替代、延期、仅适用于迁移。特别处理 TA04 的 legacy ≥4h 对照、TA06 的 crypto、TA07 legacy 回退、TA10 扩面；不能静默忽略，也不能为了收口将旧任务标 accepted。

已有 consumer 是现存用户。保留当前公开读契约、数据身份与单写入者约束，提供正常查询兼容回归；新增功能不要求改 macro-market-lab。将来有意改变消费契约时，另开有界迁移任务。

生产切换仍要核对一次实际 writer/legacy timer 所有权，避免重复派发；这是一次性运行边界核对，不是反复用旧结果当真值。发布回退优先为上一已验证的数据中心版本与兼容配置；若旧版本不适合继续拉取，方案应明确暂停生产者、保留查询并前向修复，不假定开启旧 timer 就能安全恢复。

## 4. WebUI 采用 shadcn-admin 的具体方式

按用户意图，本方案采用 [satnaing/shadcn-admin](https://github.com/satnaing/shadcn-admin)。官方源码、认证与许可证结论见配套研究；若用户指的是其它同名 fork，应先比对实际参考版本。

推荐“现 `webui` 内分步重构，以完整模板设计为视觉基线”。React/Vite/TypeScript/TanStack Table 已存在，可继续复用；保留 `lib/api.ts` 的协议和 `services` 的业务访问，重新组织 shell、路由、主题及 EURUSD 页面。不能只抄几个 Button 然后继续由每个 agent 自行设计整页。

不推荐另起永久 `webui-v2` 并长期维护两套 build/auth/发布，也不推荐直接覆盖整个目录后重接所有业务。旧页面可在迁移期间通过“更多/旧版工作区”继续访问；必要的短期独立入口只用于预览，逐页切换后删除，而非再制造一套长期产品。

### 4.1 首屏围绕任务，而不是内部子系统

导航优先“数据任务、数据查看、运行与问题、设置”。默认进入任务列表，EURUSD 详情内能看概况、原始/派生数据、运行记录、缺口、调度设置。容量/备份/接管清单放到高级运维，不占据日常操作主线。

URL 可直接定位任务和详情 tab，筛选进入 URL，刷新与浏览器后退保持上下文。引入 TanStack Router 时，要同步配置生产静态托管的 SPA fallback：当前 `api/app.py:1212` 是 `StaticFiles(html=True)`，不能假定它为任意深层业务路径返回 index；fallback 也不能吞掉 `/api` 的 404。

### 4.2 需要一份短 UI 约定

约定以参考页和可操作样例为主，涵盖：

- 固定模板 commit、MIT 版权/许可保留、来源清单；选用 sidebar、表格、sheet、dialog、dropdown、form 的实际样例。
- 全局色彩、字体、间距、密度、暗色主题；新页面复用这些，不另造一套 CSS token。
- 动画沿用模板的展开/关闭/过渡；支持 reduced-motion，任务运行状态不使用假进度动画。
- 中文主界面与统一时间展示；协议 ID 不翻译；详情给出清楚的业务名称和可复制原始值。
- loading/empty/error/pending/success、确认、字段校验、焦点/键盘、移动端操作；更新失败保留表单，后台刷新不打断编辑。
- cursor 分页沿用后端含义，不照搬模板静态数组“第 N 页”；异步提交仍由 execution/run 状态确认完成。
- TanStack Query 可用于分页缓存、刷新与 pending 管理；逐模块采用，不为套模板同时升级全部业务依赖。

前两页（任务列表、创建/详情）由用户直接验收风格，再沿同一基线铺开。视觉与业务流在同一切片中完成，不单独做一个长时间“全站换皮”项目。

### 4.3 Auth 的实际选择

shadcn-admin 默认登录、注册、忘密是演示；有可选 Clerk 示例，但不等于附送完整本地账号系统。本项目已经有 Argon2、SQLite 会话、HttpOnly cookie，以及 `/auth/login`、`me`、`logout`、`initialize`、`change-password`。

首期只换登录/账号设置的呈现，接现有后端。保留初始化和改密；不展示没有后端实现的注册、忘密、社交登录。是否要求登录后才能查看数据是独立访问策略，不能因为套登录模板顺手改变现有读权限。首期不引入 Clerk、RBAC 或另一个用户数据库。

预览采用同源 `/api` 代理，生产继续 HTTPS secure cookie；隔离本地 HTTP 预览显式设置适用的 cookie 配置与独立凭证，不能改生产 cookie 策略来解决预览登录。

## 5. 面向本项目的开发与验收流程

### 5.1 三种环境足够起步

| 环境 | 用途 | 身份与数据 |
| --- | --- | --- |
| 分支预览 | Draft PR 阶段用户即可操作；每个活跃切片独立端口/目录 | 分支+commit+是否未提交修改；独立 API/worker/scheduler/auth/ledger/canonical，默认受控源 |
| main 集成验收 | 看小 PR 合并后的累计效果；可选单独的真实 Dukascopy 沙箱模式 | 当前 main commit；独立于生产，保留用户验收数据，更新前告知并可恢复 |
| 生产 | 已验收版本长期运行 | immutable tag/deployment，真实配置与数据，按发布范围激活 |

不需要一开始引入 Kubernetes 或每个 PR 一套云环境。本机 worktree、独立端口与进程托管即可；远程访问可用现有受控通道或 SSH 转发。预览常驻到本阶段验收完成并交接，不能测试一结束便销毁，也不能把 127.0.0.1 地址当作用户远程一定可访问的 URL。

第一个工程交付应是预览管理入口，例如拟新增的 `scripts/dev-preview.sh start/status/stop`。该脚本目前不存在。它应分配独立端口、显式提供所有数据与 auth 根目录、校验没有落到生产根、启动三进程与 Vite、输出 URL/API docs/版本/模式/日志/停止方式；避免继承生产 deployment、webhook、凭证与代理等未审核配置。真实源模式须有显式的 provider/品种/窗口/请求预算。

### 5.2 每个小切片如何与你交互

1. 开工时写一页以内：这次你能完成什么、通过条件、涉及页面、范围与预览方式。只有接口/领域/访问策略的变化需要较完整 spec。
2. 尽早启动预览，发 Draft PR（在获授权的 GitHub 工作流中），不把“先通过独立 review”设成开 Draft PR 的前置条件。
3. 交付阶段卡：真实可访问 URL、分支/commit、环境、3–5 步操作、预期结果、已完成/未完成项、自动验证结果。截图是补充。
4. agent 技术 review 与用户产品验收可并行。你直接提“按钮放这里”“信息不清楚”“这个行为不对”，当前切片内修正；相关修复留在同一任务，真正扩范围再拆任务。
5. 修改后更新预览与 commit，重验受影响流程；改变了已验收行为则请你复验，不把旧 commit 的验收当作新行为通过。
6. 小 PR 合入后更新集成环境。你可继续验收累计结果；基础设施或纯后端局部改动不要求你每次操作。
7. 功能完成时做一次完整旅程验收；你确认后才准备 release。等待反馈期间可以做独立工作；未回复不等于验收通过。

建议状态用“开发中 → 可预览 → 技术可合并 → 用户已验收 → 已合并”。同一个 PR 可以同时有技术 ready 与产品待反馈，避免一个 `review` 标签混淆两种事情。功能总任务单独记录整体接受和生产状态。

### 5.3 不必积累一个巨大的功能 PR

优先小 PR 持续合 main，未完成入口用 feature flag 隔离，生产不会因为 main 变化自动更新。整体功能收口是一次验收检查点，可配一个小的收口/默认入口 PR，不必重新提交已经合并过的全部代码。

如必须保持 main 上功能完全不可见，可短期使用 feature 分支和 stacked PR，但会增加冲突、重复 review 与预览维护成本，不作为默认流程。tag 标记验收过的版本，生产激活与打 tag 分开记录。

### 5.4 Review 保留，但移到正确位置并按风险缩放

现行规则要求合并前一次独立 review，没有要求“PR 创建前才能 review”。建议保留业务代码的合并前 review；数据正确性、调度竞争与认证确实需要另一双眼睛。删掉 review 不能解决用户晚见界面的问题。

| 改动 | 开发中/预览 | 合并前 | 版本与生产 |
| --- | --- | --- | --- |
| 文案/样式/普通文档 | 快速检查、即时预览；纯文档不必起全栈 | 自查+相关 CI；可免专门独立 review，治理规则变更除外 | 并入下个已验收版本，不单独做迁移观察 |
| 普通功能切片 | 相关测试+完整可操作预览 | 一次聚焦独立 review、当前 PR verify；用户可见的新行为有阶段验收 | main 集成验收后按功能收口发布 |
| 数据身份/聚合/调度并发/权限/破坏性 schema/发布机制 | 受控场景、故障与恢复检查 | 独立深入 review、契约与风险证据；按明确风险请求确认 | 完整候选验证与有界 canary/回退计划 |

不把双轴多 agent 全量 review 当每个微调的默认仪式；修改后只复核受影响发现和新风险。对高风险改动可用 Standards/Spec 双轴。UI 风格由用户实际验收；agent review 不能代替。

本地运行相关检查；hosted 在当前提交执行合并门禁。全量本地 gate 用于跨模块/难以在 hosted 重现的风险或发布候选，不必每次微调重复 all。保留 Python 支持矩阵与正式版本全量验证；如做路径分流，统一 verify 必须正确处理跳过、失败和取消，规则本身需测试，不能让 skip 变成误通过。

CI 可减少同一分支 push 与 PR 的重复作业，并按真实依赖变化决定是否重装依赖。先优化开发反馈与重复执行，再决定是否需要进一步缩减测试。

### 5.5 Agent 总入口应怎样改

`AGENTS.md` 建议保持短小，只保留可执行默认规则，并路由到专门文档。建议采纳时调整：

| 当前条目 | 建议 |
| --- | --- |
| worktree、禁止覆写用户修改、protected main | 保留 |
| 测试数据隔离、固定依赖、secret 不输出、数据不可变 | 保留 |
| 一进门就统一 all gate | 改为“开发跑相关检查；合并 current-head verify；高风险/候选跑完整 gate” |
| 多 agent issue 认领 | 多 agent 共享任务时保留；用户直接委派的分析/小任务不强制先建立 GitHub issue；发布消息仍须有授权 |
| 小 PR/大 PR，任意生产行为即大 PR | 改为风险类型与产品验收；明确“合并代码”“更新预览”“部署生产”是三个不同动作 |
| 每个 PR 必须 review | 普通功能保留一次独立 review；低风险文案/样式自查即可；不阻塞 Draft PR/预览 |
| 所有细节与系统操作集中入口 | 浏览器安装/GitHub 排错/发布操作分别放 runbook，按任务读取 |
| 缺少日常预览契约 | 新增 URL、commit、隔离模式、可操作步骤、保留期限与反馈回路 |
| 当前事实易过期 | 状态表加 observed_at；源码实现、hosted、部署、产品验收分别标示；发布后自动生成可核对摘要 |

可以写入总入口的核心条款草案：

> 当前产品目标以当前迭代说明为准，历史迁移 spec 不自动增加本轮验收范围。用户可见改动应尽早提供隔离的可操作预览；Draft PR 和预览不要求先完成发布级验证。合并前完成与风险匹配的验证及评审，阶段产品反馈应记录并处理。Git 合并、预览更新、打 tag 与生产激活分别交付；生产操作只在已授权范围执行。旧迁移验收仅在明确开展迁移时适用；数据正确性、兼容性及单写入者约束持续有效。

正式落地需要同步改 `docs/agent-collaboration.md`、PR 模板与 CI，不能只改 AGENTS 一句话而留互相冲突的下游规则。原 spec 的取代关系也应同步明确；当前 `scripts/docs_consistency_check.py:49` 对 superseded 的后继说明使用硬编码短语，调整文档生命周期时需一起修正这个检查，不为了过门禁写无意义占位语句。

## 6. 推荐实施顺序：每步都有可见结果

| 切片 | 你能看到/操作什么 | 验收重点 |
| --- | --- | --- |
| P0 预览与范围调整 | 固定预览入口、环境/commit 标识、旧页面在沙箱内可操作；当前迭代明确只做 EURUSD | 预览根目录与生产隔离，三进程身份正确，用户可访问 |
| P1 shadcn-admin 主线页面 | 新 shell、登录、任务列表、EURUSD 创建/详情；演示数据明确标注 | 先确认喜欢的视觉、导航、字段与交互；接现有 API，不照搬假登录 |
| P2 手动 raw→5m 闭环 | 建任务、立即运行、看 1m/5m 结果与来源；复制查询请求 | 隔离真实执行全链路，固定样本正确；有界 Dukascopy 沙箱读回 |
| P3 自动维护与异常 | 定时两轮、暂停继续、重启恢复、缺口/下一次复查可见 | 真实 scheduler 端到端；源延迟/缺口/网络错误区分，不只测建 execution |
| P4 独立新增与整体验收 | 用户自行新增 GBPUSD、独立管理；按已支持 recipe 扩周期 | 非 EURUSD 写死，关联查询不丢筛选，完整用户旅程通过 |
| P5 版本交付 | 集成环境完整演示后确定版本，再做有界生产激活与观察 | 当前 commit 证据、单 writer、实际自动更新；不等待旧仓库 parity/crypto 扩面 |

P0 是首个工程交付；P1 先给你可操作界面，不等 P5。每个切片可有一个或数个小 PR，禁止又按后端全做完、前端最后集中补的方式执行。优先复用已实现模块，只为闭环发现的具体问题改代码；不先发起全局后端重构。

交付重心从“迁了多少 provider、写了多少 spec、打了多少 tag”转为“用户现在能独立完成哪些维护动作，有什么证据证明它会持续运行”。
