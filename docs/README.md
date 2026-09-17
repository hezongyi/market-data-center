# 文档入口

## 当前交付基线（2026-09-16 接续）

维护者已确认转向独立 EURUSD 维护闭环。当前工作从下列入口开始，旧迁移/扩面未完成项默认暂停排期，必要生产维护保留；这不是停止生产服务的指令。

2026-09-16 确认改为数据集中心管理，当前仍只有 Dukascopy FX / EURUSD / BID 1m→5m。先 P2.1 完成数据集手动闭环，P3 完善 EURUSD 全流程，P4 再增加 GBPUSD；其它资产、粒度和 provider 不进入本轮。

| 文档 | 用途 |
| --- | --- |
| [EURUSD 产品与 UI 规范](specs/2026-09-15-eurusd-first-product-baseline.md) | 当前目标、shadcn-admin 基线、E01–E10 验收和旧工作接续 |
| [数据集中心接续规范](specs/2026-09-16-dataset-centered-eurusd-maintenance.md) | 新管理模型、独立目录、暂停/归档语义与 DS 验收编号；与原基线一起读取 |
| [领域词汇](../CONTEXT.md) | 数据集、品种、计划、请求、执行和状态的统一含义 |
| [独立预览环境规范](specs/2026-09-15-isolated-preview-environment.md) | 环境隔离、工具契约与 PV01–PV08；实现进度统一见路线图 |
| [开发指南](development-guide.md) | 每阶段如何预览、反馈、评审、验证、合并与交接 |
| [规划指南](planning-guide.md) | 如何编写或更新 spec、切片 plan，交接给实施 agent；含模板与简化条件 |
| [实施路线图](plans/2026-09-15-eurusd-first-implementation.md) | P0–P5 顺序、实际进展及旧任务排期 |
| [shadcn-admin 本地来源](references/shadcn-admin.md) | 固定 commit、本地参考路径与更新规则 |
| [业务与流程分析](research/2026-09-15-eurusd-first-delivery-and-workflow-proposal.md)、[UI 研究](research/2026-09-15-shadcn-admin-adoption-research.md) | 调查证据与决策背景，不作为第二套实施规范 |

specs 写要求与验收，plans 写顺序和任务，research 写证据和取舍；开发流程用 developer guide，实际产品操作用 user guide。AGENTS 负责路由到当前要求，并保留不可省略的执行规则。

开发进度只在实施路线图维护；预览身份通过工具现场读回，历史执行事实放独立证据记录并引用。当前生产观察仍由 `current-state.md` 管理，不与开发进度混写。

## 运维与历史资料

阅读顺序：

1. [当前状态](current-state.md)：生产 deployment、数据 ownership、consumer 迁移矩阵和未完成事项。
2. [Operations runbook](operations-runbook.md)：部署、健康检查、维护、容量和回滚操作。
3. [WebUI 使用手册](webui-user-guide.md)：控制台各工作区的用途、操作步骤、状态语义与故障排查（面向使用者）。
4. [现行 specs](specs/)：按日期和状态查看设计规范；`superseded` 文档仅作历史背景。
5. [API/WebUI contract](api-and-webui-contract.md)、[dataset contract](dataset-contract.md)、[ingest contract](ingest-contract.md)：接口和数据不变量。
6. [Agent 协作约定](agent-collaboration.md)：issue 上报身份标注、`status:*` 标签状态机、认领与租约、agent PR 的合并门槛。
7. [integration/macro-market-lab.md](integration/macro-market-lab.md)：macro-market-lab 的读取、维护 ownership 和回滚约定。

历史接管补充：[调度器接管修复与验收规范](specs/2026-09-15-scheduler-takeover-remediation-and-acceptance.md)。迁移补证/扩面排期已暂停，缺口正确性要求由新基线继承。
对应历史证据见[调度器接管补充验收索引](scheduler-takeover-acceptance-20260915.md)，每条事实以观测时间为准。

## 状态规则

Spec 状态统一使用：`draft`、`approved`、`in_progress`、`implemented`、`accepted`、`superseded`、`blocked`。
排期独立记录 `active` / `paused` / `backlog`。暂停不改写历史实现/验收状态；局部取代在文首链接后继规范并列明范围。
历史 receipt 不修改；如当前事实变化，只更新 `current-state.md` 和新增 receipt 链接。
