# 生产任务与统一调度管理实施计划

日期：2026-09-14

状态：implementation_pending（规范第三次修订后阶段与验收已对齐；尚未执行本计划）

唯一功能依据：[生产任务与统一调度管理设计与验收规范](../specs/2026-09-14-maintenance-scheduler.md)。

本文件只记录实施顺序、代码落点、验证安排与接管步骤。任务状态、计划类型、暂停继续、数据范围、接口、兼容要求和验收结果以 spec 为准；不得通过修改本计划改变需求。AC 编号均引用 spec 第 11 节，阶段编号（S0–S5）与[调度能力分析与深化设计](2026-09-14-scheduler-capability-analysis-and-deep-design.md)第 5 节一致。

> 阶段编号说明：本文件此前只以未提交草稿存在（草稿中阶段名为 P1–P5），本次是它第一次进入版本控制，因此统一采用 S0–S5 作为唯一阶段编号。与草稿的对应关系：原 P1 拆为 S0（基座，不改外部行为）与 S1（计划注册表）；原 P2 拆为 S2（时间与影子调度）与 S3（真实派发）；原 P3 成为 S4（依赖闭环）；原 P4、P5 合并为 S5（WebUI、接管与回滚）。拆分理由见评审文档第 5 节。

## 1. 开工与交付边界

- 实施前重新核对最新 origin/main、实际相关代码和生产入口；不把初次调研基线当作未来开工基线。
- 按 AGENTS.md 和 agent 协作约定建立、认领对应 issues，从最新 origin/main 创建干净独立分支/worktree，保护现有未提交改动。
- S0 不改动任何外部行为（无新路由、无新进程、无调度决策），可独立合并；S1 起才引入用户可见的计划管理。
- 开发阶段 scheduler 保持未激活；S1 的写接口可用但不派发，S2 以 `dispatch_enabled=false` 运行影子模式，S3 起才真实入队。
- 隔离验证使用 fixture 与受控 provider；生产动作按发布清单另行执行。
- `retention-audit` 与 provider acceptance 的解耦（spec §9 第 3 条）是**先行独立修复**：它不属于本计划的交付批次，但必须在 S5 接管前以独立小发布完成并留 receipt；不得并入接管批次，也不得因为“接管时会一起处理”而推迟。

## 2. 阶段与验收映射

| 阶段 | 主要代码落点 | 实施工作 | 对应验收 |
| --- | --- | --- | --- |
| **S0 基座** | `runs/ledger.py`、`deployment.py`、`operations_views.py`、`scripts/*`、`backend/tests/` | 顺序迁移与 schema 版本；WAL 与有界 busy 处理；批量幂等原子入队；注入时钟；`jobs` 归属列（计划/execution/step）与到期索引；`runs` 可索引列 + SQL 侧读模型；deployment 逻辑哈希表清单与 `service_names`；receipt action 登记；多进程争用与备份恢复回归 | AC05 基础、AC22、AC24 的基础部分 |
| **S1 计划注册表** | 新增 production task module、`maintenance_tasks.py`、`api/app.py`、`catalog/snapshot.py`、`webui/src/lib/api.ts`、`webui/src/pages/MaintenancePage.tsx` | 计划/配置版本/所有权表与唯一约束；`preview/create/read/change`（含 pause/resume/run_now/archive/copy/delete/acknowledge_drift）；alias 兼容；`/production/*` 路由与 `MUTATING_ROUTES` 分类；`/capabilities` 增补；计划列表与详情 UI；目录矩阵（已计划/未计划/不可用/漂移）与只读清单 | AC01、AC07、AC16、AC19、AC20、AC23 |
| **S2 时间与影子调度** | 新增 `scheduler_main.py`、任务模块、`deploy/systemd/market-data-center-scheduler.service`、`api/app.py` | 计划时间模型（manual/once/fixed_rate/fixed_delay/daily，注入时钟）；`reconcile()` 决策路径；lease + fencing；`/operations/scheduler`；tick receipt；以 `dispatch_enabled=false` 记录“此刻会派发哪些计划/窗口/步骤” | AC02、AC03、AC14 |
| **S3 真实派发与执行** | `ingest/worker.py`、`runs/ledger.py`、任务模块 | execution/step/progress 持久化；按归属列与暂停状态反连接领取；run_now/retry 与 `/runs/{id}/retry` 归属联动；coalesce、backlog、预算与公平轮转；provider 退避；单 worker 派发上限（默认 1，可配置） | AC04、AC06、AC08、AC13、AC15 的部分 |
| **S4 依赖闭环** | `platform.py`、`maintenance_runner.py`、`derived_maintenance_runner.py`、`transform.py`、`ingest/process.py`、`catalog/snapshot.py` | 提取复用 planners；动态窗口与 backlog；固定输入 part 引用与跨进程重建；recipe 拓扑依赖与环检测；publication 游标与 reconciliation；受影响范围重算；旧 CLI 接同一任务模块 | AC09–AC12、AC15、AC18 的跨 provider 部分 |
| **S5 接管与默认启用** | `deploy/systemd/`、`deployment.py`、`scripts/browser_acceptance.cjs`、runbook、release checklist、`docs/current-state.md` | 浏览器端到端闭环与移动端；旧入口盘点与暂停导入；阻新、收口、核对 ledger 后启用 canary；观察、回滚演练；文档与证据收口（含当前状态与部署身份） | AC15、AC17、AC18、AC21、AC24 |

表中后端文件相对 `backend/src/data_center/`，前端页面相对 `webui/src/pages/`。模块内部拆分可依实现调整；不改变 spec 的外部契约。

AC04 放在 S3 而不是 S2：影子模式不派发、不产生 execution，因此“两个 scheduler 竞争只接受一个 execution”在 S2 无法验证；S2 只验证计划时间、决策正确性与有界性。

## 3. S0 基座检查点

1. 先落迁移机制：顺序迁移列表 + schema 版本记录 + 打开时校验，并补“从当前生产库（无版本记录）升级”和“迁移可重跑”的测试；DDL 不再分散在各方法体内由每个进程执行。
2. 打开 WAL 与显式的 busy 处理，写事务保持毫秒级；补多进程（不是线程）争用测试，断言无 `database is locked` 失败、无长写锁。
3. 提供同一连接、同一事务的批量幂等入队；用故障注入覆盖“部分窗口插入时崩溃”和“事务成功但响应丢失后重试”。
4. 为到期扫描与领取建索引；把暂停判断的前缀匹配替换为归属列，并保留旧数据的兼容读取。
5. 为 `runs` 增加可索引列并在同一事务内与 payload 一起写；提供按计划/执行/步骤分页的 SQL 侧读模型，**不改动**现有 `/runs` 契约。
6. 注入时钟覆盖计划时间、心跳与退避；测试不得依赖真实等待。
7. 把新增调度表加入 deployment 逻辑哈希的表清单，把 scheduler 服务加入 `service_names`，把调度 receipt 的 action 登记到运维 receipt 视图的 action 列表。
8. deployment 逻辑哈希按已确认方案实现（见 issue #107）：
   - **所有新增调度表都纳入**哈希清单（`production_plans`、计划版本、所有权、进度、输入引用、execution、step、`step_runs`、`scheduler_state`、`scheduler_leases`）；不建立排除清单，避免"激活未触碰数据"的证据出现盲区。
   - 把实现改为**按 `rowid` 顺序流式**遍历，去掉现有的全列 `ORDER BY` 与临时排序；在 stage/activate receipt 的 details 中记录**每表行数与耗时**，使激活成本可观测。
   - **不引入删除式保留策略**：与 spec 不变量 12 及"永不删除 ledger/receipt/canonical"的规则冲突。
   - 验收：在 1,000+ runs/jobs 与至少 100 条 execution 的合成库上，单次哈希耗时相对现状不劣化，且 receipt 中能看到逐表行数与耗时（对应 AC22）。
   - 退路（仅在实测证明成本仍不可接受时启用）：小状态表逐行精确 + 追加型历史表用 `(count, max(rowid), 尾部摘要)`；该哈希只在同一次 stage/activate 内前后比较，换算法不影响任何已保存的值，但必须在 receipt 中记录所用算法。
9. 备份/恢复回归必须覆盖新增表与输入引用；确认恢复流程不覆盖既有目标的行为不变。

## 4. S1 计划注册表检查点

1. 建计划、配置版本、所有权三类持久化结构；所有权键在 `enabled/paused` 计划间唯一（以部分唯一索引实现，SQLite 不支持在 `CREATE TABLE` 内写带 `WHERE` 的 UNIQUE），冲突返回稳定错误码，归档与删除在同一事务释放。
2. 接 `preview`、`create`、`read`、`change`；`change` 的命令集合以 spec §8 为准（`update | pause | resume | run_now | retry | archive | copy | delete | acknowledge_drift`），全部支持 `expected_version` 与幂等键；编辑不改变 `desired_state`。
3. 删除按 spec 5.4 实现：前置条件、墓碑、审计（含被拒尝试）、alias 解析；canonical 数据与历史不动。
4. 新路由登记进 `backend/tests/test_api_surface_contract.py` 的 `MUTATING_ROUTES`；新增单元的 `EnvironmentFile` 必须位于机器级配置目录下。
5. 配置版本持久化解析时的配置摘要，并实现 registry 漂移检测（`health=config_drift`、停止派发、`acknowledge_drift` 后才能恢复）。
6. WebUI 落在 Maintenance 工作区内（生产计划 / 单次维护两个入口）；`schedule` 不再是 `Literal["manual"]`；计划列表用新的 cursor 分页接口，不复用旧的无参列表。
7. 目录矩阵与只读清单按 spec 3.4 实现：观测投影、非配置来源、报告“仓库声明 vs 主机已安装”差异。

## 5. S2–S3 检查点

1. 先用测试时钟验证时间计算（含 DST 缺失/重复时刻、misfire、合并触发），再接 SQLite 竞争与事务故障注入，最后启动持续运行进程。
2. 影子模式（S2）以 `dispatch_enabled=false` 运行至少一个完整生产周期，产出“某时刻会派发哪些窗口/步骤”的可核对 receipt，并与现有 timer 的实际窗口对照；差异必须解释清楚后才进入 S3。
3. 单独验证 pause/claim、全局暂停、lease 接管与 worker 恢复的交互；使用不同连接或进程，不能只串行调用假装并发。
4. 逐步提取现有 runner 的纯规划逻辑（`evaluate_coverage`、`plan_maintenance`、`plan_tail`、`build_ingest_plan`、`ingest_window_payloads`、`_tail_recovery_windows`、`_exclude_planned_windows`），保留 gap isolation、session clipping、availability lag 与 cooldown 的现有行为证据。
5. 把执行计划、步骤与发布进度持久化；测试“完成 raw 后进程退出”“派生缺失”“手工 repair”“新输入到达”“长期停机超过 2 天与 31 天”五类场景。
6. 接固定输入执行与 recipe 拓扑依赖；用现有 transform executor 做实际隔离发布/readback，覆盖局部 gap 与周/月上游依赖；固定输入必须在新增 raw part 后仍可重建（spec 6.3）。
7. 补列表与到期检索索引、轮转调度、provider 退避和观测投影；验收有界性能与公平性后再固定调优值。
8. 并发上限作为计划与全局的策略字段（默认 1）；提升到大于 1 之前，先完成 WAL 压测、每窗口幂等证据与 provider 退避验证。

初始负载实验采用 tick=5 秒、单次最多检查 50 个到期任务、全局最多预取 16 个步骤；每任务派发上限遵循 spec。以上是实现调优起点，不是额外功能契约；参数与测试结果写入 receipt，调优须保持 AC14。

## 6. S4 依赖闭环检查点

1. 用 registry 的 `input_recipe_id` 与 `input_dataset` 建真实拓扑（含环检测与自动补 1d 中间依赖），不再按“层”循环并逐层重新解析 catalog。
2. 持久化输入 part 引用（相对路径、run_id、schema 版本）并实现跨进程重建；执行不再以“当前 catalog 恰好未变”为前提。
3. publication 游标与 reconciliation：raw 发布后进程退出、raw 无需更新但派生缺失、外部受治理手工修复三类情况都必须补上派生或重算。
4. gap 修复后的受影响范围进入持久化重算集合，逐层更新 lineage；重算必须有预算，不能每 tick 全历史重算。

## 7. S5 WebUI、接管与回滚

### 7.1 浏览器端到端

- 依据后端 capabilities 实现字段、产物和操作可用性；沿用现有语言及全局时间显示机制。
- 以一个 fixture 任务走通创建、等待到期、raw/derived 查询、暂停、进程重启、继续、归档/删除与历史关联。
- 以第二任务验证独立暂停、全局继续、冲突、所有权拒绝与失败隔离，补移动端与详情阅读验收。
- 对无法精确预测的完成时间、持续增长的 backlog 和 provider lag 验证文案，避免误导进度与时效。
- 使用 pinned Playwright、隔离 canonical/ledger/evidence/backup 和仓库标准浏览器运行方式。

### 7.2 接管执行顺序

以下步骤落实 spec 第 9 节；生产执行前完成所需发布与外部入口协调。

1. 保存旧 raw/derived timers 和 service 的实际身份、触发规则、allowlist、recipes、范围、scope、运行中作业与回滚配置；包含文档提及的 macro-market-lab 派生入口，并以主机实际安装的单元为准。
2. 将旧配置导入暂停任务，生成新旧规划对照 receipt；核对原始层 fixed_delay 与有效 policy，确认没有扩展生产范围；首版只覆盖当前已有周期生产者的产物（原始 1m 与 1m→5m 派生）。
3. 完成增量 schema、scheduler service 与 immutable deployment 的隔离启动/失败回退/重启演练，并验证备份恢复。
4. 阻止旧入口新提交，等待在途窗口收口，核对 ledger 和生产所有权；随后启用 canary 新任务。
5. 对 crypto 与 FX 任务验证实际生产、readback、WebUI 控制、调度和 deployment identity，按原周期/provider lag 留存观察证据，再扩大至原批准范围。
6. 演练暂停新派发、在途收口、旧 binary 兼容处理和恢复旧入口，确认不会领取暂停任务或双重调度；保存回滚与前向恢复 receipts。
7. 确认 `retention-audit` 已作为独立单元运行、拥有自己的 receipt action 并已登记到运维 receipt 视图。
8. 确认派生入口已改用 release venv 与 deployment manifest，不再引用仓库 checkout。
9. 更新 runbook、service 安装说明、版本兼容资料、`docs/current-state.md`（当前状态与部署身份）与发布证据；只有 spec 全部 AC 有证据后才关闭功能交付。

## 8. 验证与评审证据

| 层次 | 执行安排 |
| --- | --- |
| 模型与时间 | 注入时钟验证版本、状态、UTC/DST、未来启动、misfire；临时 SQLite 验证索引与唯一约束 |
| 事务与恢复 | 多进程竞争、lease 过期、事务中断、响应丢失、pause/claim 竞争、worker staging 恢复、批量入队故障注入 |
| 基座与迁移 | 从当前生产库升级、迁移重跑、WAL 下争用压测、备份/恢复含新表与输入引用、deployment 身份覆盖调度表且激活成本有界 |
| 数据集成 | 隔离 worker 执行 raw/derived、固定输入、coverage、gap repair、backlog 和查询 readback |
| 浏览器与服务 | AC15 用户流程、任务独立性、调度与 worker 重启、移动端；不联系真实 provider |
| 统一 gate | `bash scripts/ci.sh all`，Python 3.10/3.11/3.12、Node 22、Ruff、兼容/依赖/secret/operations/browser/service 验证；注意本机 shell 若带 `NODE_ENV=production`，`npm ci` 会跳过 devDependencies（见 issue #106），需在干净环境下运行 |
| 生产接管 | 按 7.2 取得实际旧入口退出、新任务接管、观察、身份和回滚 receipts，独立于隔离 CI |

每个阶段的 PR 关联原 spec、阶段及 AC 编号，提交风险、已完成/未完成项与对应 commit 的验证结果。遵守 AGENTS.md 的评审及两级合并门禁；不以旧 commit 的成功结果代替当前 head。

## 9. 进度记录

- 2026-09-14：原扩展规划中的功能、状态、接口、数据与验收要求已归入原 spec；本文件精简为实施步骤。
- 2026-09-14：规范完成第二、三次修订（缺口 G1–G14、四项已确认决策、AC19–AC24）。本文件随该修订第一次进入版本控制，采用 S0–S5 阶段编号并给出与原未提交草稿 P1–P5 的对应关系；新增 S0 基座与 S1 注册表检查点、S4 依赖闭环检查点，把 `retention-audit` 解耦列为接管前必须完成的独立工作项，并在接管步骤中补入 receipt action 登记与 `docs/current-state.md` 更新。
- S0–S5 尚未据本计划实施或验收，未执行生产接管；后续以实际 commit、PR 与 receipt 更新进度。
