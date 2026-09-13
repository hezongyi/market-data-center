# WebUI 数据维护工作台 v0.4 规划

日期：2026-09-13  
状态：in_progress  
基线版本：v0.3.3  
相关文档：`docs/api-and-webui-contract.md`、`docs/specs/2026-09-11-webui-modernization.md`

## 背景

v0.3.3 已完成 WebUI 运维控制台的第一轮现代化，具备 Overview、Data Catalog、Runs、Quality、Explorer 和 Operations 六个工作区，并通过统一 typed API client 访问 `/api/v1`。

后端已经具备原始行情、派生行情、经济数据 current/PIT、异步 ingest、derive、quality check、retry、dead-letter、coverage、manifest、容量保护和备份恢复等能力，但当前 WebUI 只覆盖其中一部分。下一阶段需要把 WebUI 从“观察系统状态”提升为“发现问题、发起维护、跟踪结果、验证数据”的数据维护工作台。

## 目标

1. 让用户可以通过 WebUI 管理现有数据资产，而不需要直接调用 API 或操作本地文件。
2. 建立从 coverage/quality 发现到维护任务再到结果验证的闭环。
3. 保留现有 `/api/v1` envelope、鉴权、cursor、snapshot、schema 和容量保护语义。
4. 让每个写操作都具有可解释、可追踪、可恢复的状态。
5. 在不更换 React/Vite 技术基线的前提下，渐进扩展现有 WebUI。

## 非目标

- 不重写现有 React/Vite 应用或引入完整后台产品框架。
- 不在浏览器直接读取 Parquet、SQLite、canonical、ledger 或 evidence 路径。
- 不把 cursor 查询转换为 offset/total 分页。
- 不在 v0.4 引入完整 RBAC、主题编辑器或通用图表平台。
- 不虚构后端没有提供的告警、质量或 provider 状态。

## 用户任务模型

WebUI 的主流程应支持：

```text
发现数据状态
  → 查看 coverage / quality / lineage
  → 创建 ingest、backfill、gap repair、derive 或 quality 任务
  → 观察 queued / running / pass / degraded / failed
  → 查看 manifest、snapshot、错误阶段和 findings
  → 再次查询验证结果
```

## 信息架构

### 数据资产

- **Data Catalog**：dataset、schema、kind、provider、partitioning、coverage 和质量状态。
- **Explorer**：provider bars、market bars、economic current/PIT 查询；保留 snapshot、schema、recipe 和 cursor 元数据。
- **Coverage**：覆盖区间、ready intervals、gap count、coverage scope 和可维护区间。

### 数据维护

- **Maintenance Center**：统一创建 ingest、backfill、gap repair、derive、quality 和 parity 任务。
- **Task Templates**：保存经过校验的常用 selector、时间窗口、provider、recipe 和 run scope。
- **Validation Preview**：提交前显示参数校验、预计窗口数、容量保护和潜在风险。

### 运行治理

- **Runs**：按 status、dataset、run kind、run scope 和时间筛选；展示运行阶段、窗口、重试链和 dead-letter。
- **Run Detail**：展示请求参数、输入 snapshot、输出 manifest、schema、质量结果、错误阶段和关联 finding。
- **Quality**：按 severity、code、dataset、selector 和时间筛选；支持从 finding 创建修复任务。

### 平台运维

- **Overview**：readiness、worker、容量、24 小时运行统计和开放 finding。
- **Operations**：队列、容量、备份、恢复、deployment identity、写保护和操作审计。

## 前端架构

继续使用 React 19、TypeScript、Vite、TanStack Table 和 Lucide。现有 AppShell、设计 token 和共享 UI 组件作为基础保留。

页面不得直接实现 fetch、envelope 解包、API key 注入、cursor 处理或 mutation 状态机。目标调用链为：

```text
Route / page
  → query 或 mutation hook
  → domain service
  → typed data-center client
  → /api/v1
```

建议的领域服务：

- `catalogService`：datasets、coverage、capabilities。
- `queryService`：provider bars、market bars、economic observations。
- `runService`：runs、run detail、manifest、retry、acknowledge。
- `maintenanceService`：ingest、economic ingest、derive、backfill、gap repair、quality check。
- `qualityService`：findings、finding detail、修复任务。
- `operationsService`：ready、metrics、capacity、backup 和审计信息。

统一状态模型：

```text
查询：idle | loading | success | empty | error
写操作：draft | validating | confirming | queued | running | pass | degraded | failed
权限：authorized | unauthorized | protected
新鲜度：fresh | stale | unknown
```

## 领域模型

### Dataset

至少包含 `dataset_id`、`schema_version`、`kind`、`description`、`partitioning`、provider capability 和 lineage 信息。原始 `provider_bars`、派生 `market_bars` 与 `economic_observations` 必须在 UI 中明确区分。

### MaintenanceTask

```text
task_id
run_id / run_ids
dataset_id
run_kind: ingest | derive | backfill | gap_repair | quality | parity
run_scope
selector
time_range
recipe / price_basis（适用于 market_bars）
status
created_at / started_at / finished_at
```

### RunResult

除现有字段外，UI 需要能够读取并展示 `stage`、`window_count`、`input_snapshot_id`、`manifest`、`finding_count`、`degraded_reasons`、`retry_of` 和 dead-letter 状态。terminal run 不可被 UI 覆盖。

### Finding

Finding 应关联 `dataset_id`、selector、时间位置、`run_id`、severity、code、message 和处理状态。处理状态与运行结果分开，避免把 acknowledge 误认为问题已修复。

## API 配套要求

现有接口继续兼容。为支持本规划，优先补充或明确以下契约：

1. `/runs` 支持 status、dataset、run_kind、run_scope、时间范围和受控分页。
2. `/market-bars/coverage` 提供派生行情覆盖与 recipe 信息。
3. derive、economic ingest、quality check 和 backfill 返回统一的 queued Run envelope。
4. Run detail 明确阶段、输入 snapshot、输出 manifest、degraded 原因和窗口统计。
5. Findings 提供稳定的关联字段和处理状态。
6. 提供 provider、instrument、timeframe、recipe 的 capability 元数据，供表单校验和禁用不可用选项。
7. 写接口错误统一返回安全的 `code`、`message`、`request_id` 和容量/权限语义。

如果后端接口暂时不能扩展，前端必须显式标记能力不可用，不得通过猜测或本地计算伪造状态。

## 分阶段范围

### Phase 1：维护任务闭环

- 统一 provider bars、economic ingest 和 derive 表单。
- 支持 run kind、run scope、时间窗口和提交前预览。
- Runs 展示阶段、窗口、重试链、manifest 和错误阶段。
- 写操作统一提供确认、pending、queued、成功、degraded、失败和保护状态。
- 接入 economic ingest、derive 和 quality check。

验收：用户可以从 WebUI 创建一项维护任务，跟踪其 Run，并查看结果和 manifest；未经鉴权或容量保护时不会发起写请求。

### Phase 2：数据资产工作台

- Catalog 展示原始/派生关系、provider capability 和 coverage 质量状态。
- Explorer 支持 `provider_bars` 与 `market_bars`。
- 展示 ready intervals、gap count、coverage scope、recipe、price basis 和 snapshot。
- 从 coverage 或查询结果直接创建维护任务。

验收：用户可以从一个缺口区间直接进入 gap repair/backfill 或 derive 流程，并在结果页回查同一 snapshot 语义。

### Phase 3：质量反馈闭环

- Quality 支持结构化筛选和 finding detail。
- finding 关联 Run、manifest 和数据区间。
- 提供“创建修复任务”入口。
- 明确区分 `failed`、`degraded`、`coverage_not_ready`、`provider_gap`。

验收：用户可以从 finding 创建有边界的修复任务，并确认修复后的新 Run 与原 finding 的关系。

### Phase 4：运维与审计增强

- 展示维护队列、worker 事件和历史容量状态。
- 展示备份、恢复演练和 deployment 变更记录。
- 记录操作人、时间、selector、任务类型和结果。
- backfill 提交前展示预计窗口数和容量影响。

验收：关键写操作有可追溯记录；容量 warning/critical 能在提交前阻止或限制高风险任务。

## 设计原则

- 只读 Explorer 与高风险维护操作保持清晰分离。
- 状态同时使用文字、颜色和图标表达，不能只依赖颜色。
- 所有时间使用 UTC；run ID、snapshot ID 和 commit 使用等宽字体。
- loading、empty、error、unauthorized、protected、degraded 都是完整设计状态。
- 优先保证表格、筛选、详情和操作路径；图表在数据契约稳定后再增加。
- 移动端保持任务表单单列、详情抽屉可读、导航可达；桌面端保持高密度扫描。

## v0.4 验收标准

- `npm --prefix webui run build` 通过。
- 现有隔离 Playwright 验收继续通过，并覆盖 1440px 与 390px。
- 维护任务覆盖 provider ingest、economic ingest、derive 和 quality check。
- 查询保留 cursor、snapshot、schema_versions 和 query mode 语义。
- 每个可变操作均验证鉴权、容量保护、确认、pending、queued、成功和失败路径。
- terminal Run 不被 retry 或前端刷新覆盖。
- 不读取生产文件系统，不把 API key 写入 URL、日志或持久化存储。
- 新增 API 契约有对应 backend contract test 和 browser acceptance。

## 实施决策

- 采用渐进式领域拆分，不更换现有前端技术栈。
- “统一维护任务中心”是 v0.4 的第一实施目标。
- 先完善任务和 Run 契约，再扩大图表、批量操作和权限能力。
- 后端缺口以 API contract 变更配套实现，不在前端建立临时旁路。

