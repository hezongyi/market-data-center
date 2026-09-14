# 生产任务与统一调度管理设计与验收规范

日期：2026-09-14
状态：in_progress（需求规范已修订；生产任务调度闭环尚未实施验收）
前置版本：WebUI v0.5 任务中心

本文件是生产任务与统一调度管理的唯一功能规范，沿用原 Maintenance Scheduler 的文件路径与引用。本文规定功能、状态、数据和接口契约，以及完成门槛；[实施计划](../plans/2026-09-14-production-task-scheduler.md)只规定实施顺序、代码落点和验证安排。[调度能力分析与深化设计](../plans/2026-09-14-scheduler-capability-analysis-and-deep-design.md)记录独立评审的证据、缺口（G 编号）与落库设计；需求冲突时以本文件为准。

关联规范：

- [WebUI v0.5 任务中心](2026-09-13-webui-v0.5-usability-and-access.md)
- [市场数据平台](2026-09-11-data-center-market-data-platform.md)
- [Dukascopy 1m BID](2026-09-11-dukascopy-1m-bid-rollout.md)
- [Market bars 派生与 consumer 切换](2026-09-11-market-bars-derivation-and-macro-cutover.md)

## 1. 背景、目标与范围

v0.5 已提供维护任务注册、手动提交、状态展示和有限的暂停恢复。原规范仅规定 manual/hourly/daily 的可靠入队与恢复；本次扩展为用户可通过 WebUI 持续管理指定品种的生产目标，并将原始和派生数据纳入统一调度。

在现有 SQLite ledger、window planner、worker、catalog 和 transform executor 上增加持久化生产任务模块。WebUI 管理长期目标，scheduler 决定何时推进，现有 worker 执行数据窗口。系统必须支持：

1. 创建指定品种的生产任务，配置原始数据、派生产物、数据范围、首次启动时间和重复计划。
2. 到期触发只接受一次逻辑执行，多个 scheduler 竞争、响应丢失和服务重启不产生重复提交或丢失进度。
3. 当前窗口结束后暂停，继续时恢复未完成工作；错过周期不丢失待补数据范围。
4. provider_bars 发布后根据范围 readiness 和 recipe 依赖推进 market_bars，并在 gap 修复后重算受影响产物。
5. 在 WebUI 统一查看任务、调度状态、进度、历史、关联 run、数据时效和阻塞原因。
6. 继续遵守容量、身份、审计、不可变发布和既有 /api/v1 envelope，完成现有原始/派生 timer 的受控接管。
7. 提供“已注册生产计划”的唯一清单：计划身份、所有权、目标产物、数据范围、节奏、状态与健康度都能被查询，并能回答“registry 里已批准的品种与 recipe 有哪些没有计划在维护”。

统一管理的范围是行情生产任务：原始维护、历史补齐、缺口修复及依赖派生。首个生产验收对象为 Dukascopy 1m BID 与已注册的 market_bars recipes；通用模块复用须通过 fixture 或第二 provider 的隔离验收。首个接管批次只覆盖当前已有周期生产者的产物（原始 1m 与 1m→5m 派生）；其余已注册 recipe（15m/30m/1h/4h/1d/1w/1mo）只作为计划中可选、按需创建的能力，不随接管默认启用、不默认回补历史。备份、监控、发布和 provider acceptance 的 timer 继续独立治理，但必须以只读清单出现在统一调度视图中（见 3.4），不产生接管、不改动其生命周期。

### 非目标

- 不引入通用工作流平台、复杂 cron、任意 DAG/脚本编辑、多租户或新的角色体系。
- 不引入 Redis、Celery、Airflow、多机部署或并行数据 worker；多个 scheduler 实例的去重与接管仍须验收。
- 不新增未经批准的品种、ASK/MID 数据语义或未注册 recipe；品种注册继续遵守控制面治理。
- 不改变 canonical identity、现有 UTC 数据语义或历史 terminal receipts，不自动删除数据以应对容量不足；删除只作用于计划定义，永不删除 canonical part、manifest、run、receipt 或审计记录（见 5.4）。
- 不移除 v0.5 单次手动维护；不把已有手动任务或历史 run 自动转换成持续生产。
- 不提供强制中断当前窗口或取消单轮的产品操作；首版暂停采用安全窗口边界。
- 不接管 monitor、smoke、provider acceptance、backup/restore、release 等治理型 timer 的生命周期，只在清单中展示其所有者、节奏与最近 receipt。

### 已确认的设计决策（2026-09-14）

以下四项已由维护者确认，实施与评审均以其为准；细节分别落在括号内的章节。

1. **统一视图纳入治理型单元，但只读**（3.4）：清单同时显示生产计划与治理型单元（monitor、smoke、provider acceptance、backup/restore、release），后者只有所有者、节奏与最近 receipt，调度器对其无启停、改写或重排权限。纳入的理由是视图可信度与缺陷可见性：只显示生产计划而主机还跑着其它定时任务，等于向运维隐瞒事实；反之，“某个治理单元最近 receipt 已是数天前”这类缺陷会自然浮出。若将来出现“顺手启停治理单元”的需求，属于范围变更，必须回到本节重新决策。
2. **首个交付批次保持每 ledger 一个数据 worker，并行度是可配置策略而非硬编码**（7.3）：串行吞吐量级可接受（8 品种 × 31 天 × 60 分钟分片约 5,950 个窗口，按每窗口 3–10 秒估算约 5–17 小时的一次性回补），而并行的真实瓶颈是 SQLite 写竞争、provider 限流与唯一发布路径，必须在幂等与所有权经过真实并发验证之后才引入。重新评估并行的触发条件：需要多年历史、需要同时维护多个高周期产物，或影子与灰度数据证明串行无法满足新鲜度。
3. **新建周期计划默认 `fixed_rate`，只有旧 timer 导入使用 `fixed_delay`**（5.1）：可预览的确定时刻、可度量的到期延迟与可定义的合并语义都要求锚点式计划；防重叠由“每计划至多一个非终态 execution + provider 延迟 + 合并触发”保证，不依赖相对时间。若观察数据表明单轮耗时经常超过周期，可把原始层默认改为 `fixed_delay`——这是按计划可调的字段，不是架构决策。
4. **`retention-audit` 与 provider acceptance 的解耦作为先行独立修复**（9 第 3 条）：它不属于本功能交付，但在接管前必须完成并以独立 receipt 留证；不得等到调度器接管时才处理。

## 2. 当前基线与差距

调研基线：`180dc73fcb2931ef277923360f05e6174964a86b`（WebUI v0.5 后的本地已提交基线）。下列证据描述修订时的既有实现；后续实现须重新核对最新受保护主分支。运维文档中的部署记录为历史证据，不代替实施前的主机状态盘点。

| 现有能力 | 源码或文档证据 | 需要补齐 |
| --- | --- | --- |
| 手动请求必须带固定 start/end，schedule 只接受 manual | `backend/src/data_center/maintenance_tasks.py:80-83` | 长期任务需要“数据窗口策略”，不能周期性重放旧请求中的固定日期 |
| 任务提交先逐个 enqueue，再 upsert 一份任务 payload，保存本次 run_ids | `backend/src/data_center/maintenance_tasks.py:433`、`:499-506`；`backend/src/data_center/platform.py:128-134` | 长期定义、每次执行和历史运行需要独立记录；现有逐次提交不能直接充当原子调度事务 |
| 任务表只有 task_id、payload、status、updated_at；展示状态由关联 run 再推导 | `backend/src/data_center/runs/ledger.py:21`、`:92-96` | 需要分开“用户是否启用”和“本轮是否成功”，不能共用一个 status |
| 暂停通过 job_id 字符串前缀阻止 queued 作业领取 | `backend/src/data_center/runs/ledger.py:317`；`backend/tests/test_ledger_reliability.py:20` | 已有有限的暂停基础，但需要精确 task_id 关联，避免相似名称误匹配，覆盖分片和 retry |
| 原始维护由 systemd 触发 maintenance_runner，经 API 提交并等待 worker | `deploy/systemd/market-data-center-1m-maintenance.timer:5-7`；`backend/src/data_center/maintenance_runner.py:399-401` | 现有周期是“上轮结束后 15 分钟”，不是固定每刻钟；任务 registry 尚未成为它的控制入口 |
| Dukascopy policy 为 2 天 tail、60 分钟分片、180 分钟可用延迟与 gap cooldown | `backend/src/data_center/platform_registry.py:43` | 新任务默认继承这些治理参数，WebUI 显示有效参数和预计数据时效 |
| 已有 snapshot-aware 派生 runner，逐层解析 recipe、提交 derive、记录 receipt | `backend/src/data_center/derived_maintenance_runner.py:132`、`:200` | 提取、复用其规划逻辑，补持久化步骤和自动依赖推进 |
| 运维记录指出派生曾由 macro-market-lab 的 systemd service 调用 Data Center runner | `docs/specs/2026-09-11-market-bars-derivation-and-macro-cutover.md` 末尾 | 接管时必须盘点两个项目的旧入口，不能只停本仓库原始层 timer |
| worker 以每 ledger 的 flock 保证单 supervisor，并恢复 staging/publication | `backend/src/data_center/ingest/worker.py:70-78`、`:141-164` | scheduler 多实例去重与数据 worker 并行是两件事；首版保留单 worker |
| derive 入队及执行时都要求 snapshot 与当前 catalog 一致；snapshot 缓存有进程内 TTL | `backend/src/data_center/ingest/worker.py:54-68`；`backend/src/data_center/ingest/process.py:55-61`；`backend/src/data_center/catalog/snapshot.py:41,121-124` | 长时间排队和暂停恢复需要持久化输入引用，不能只存一个可能无法重建的 snapshot_id |
| recipes、approved instruments、session/calendar、coverage 和容量策略已经存在 | `backend/src/data_center/platform_registry.py`；`docs/operations-runbook.md` | scheduler 复用这些事实，不重新解释交易时段、BID 或质量规则 |
| 生产计划定义分散在三处：代码 registry（品种/recipe/policy）、机器级 env `DATACENTER_MAINTENANCE_SYMBOLS`、systemd timer（节奏） | `backend/src/data_center/platform_registry.py:41-45,103-123`；`backend/src/data_center/settings.py:41,82-89`；`deploy/systemd/market-data-center-1m-maintenance.timer:5-7` | 需要单一计划注册表：品种范围、产物、范围、节奏、治理参数都是计划数据，不靠部署改动 |
| 生产实际在跑的行情周期计划只有两条：原始 1m 维护、1m→5m 派生；15m/30m/1h/4h/1d/1w/1mo 已注册但无任何周期生产者 | `deploy/systemd/market-data-center-1m-maintenance.timer`；`/home/quant/repos/macro-market-lab/scripts/marketlab-maintain-market-bars-data-center.sh:9-11,25-30` | 接管首版只覆盖这两条；其余 recipe 是按需创建的能力，不随接管默认启用 |
| ledger 无 WAL、无 `busy_timeout`、无 schema 版本，全库只有 1 个非主键索引；`jobs` 无 `(status, available_at)` 索引，领取时把全部 queued 行读入 Python | `backend/src/data_center/runs/ledger.py:17-31,317-335`；生产实测 `journal_mode=delete`、`user_version=0` | 需要基座前置：WAL、显式 busy 处理、顺序迁移、到期索引、批量原子入队（见 7.4） |
| run 读模型把全部 runs 载入内存再过滤分页 | `backend/src/data_center/run_views.py:148,181,186,303`（生产 1117 runs 实测约 400 ms 全量读取） | 计划历史与执行查询必须走 SQL 侧列与索引，不能建立在全量读模型上 |
| deployment 逻辑哈希写死了审计表清单 | `backend/src/data_center/deployment.py:438` | 新增的调度状态表必须进入该清单，否则部署身份看不见调度状态 |
| 派生执行以“重新解析当前 catalog 并与提交时 snapshot_id 比较”保证输入一致，失败为 `ValueError` 且 `retryable=False` | `backend/src/data_center/ingest/process.py:35,55-61` | 需要持久化 part 引用并能重建执行输入，否则派生排队期间 raw 推进一次即永久失败（见 6.3） |
| 真实 provider 验收自 2026-09-11T03:39 起连续失败，其 `ExecStartPost` 的 `operations retention-audit` 随之停跑（最近 receipt 2026-09-11T03:58） | `deploy/systemd/market-data-center-provider-acceptance.service:12`；`acceptance/receipt-*.json` | 计划的健康度与依赖需要被显式管理；接管清单中必须把保留审计与验收解耦（见 9） |
| macro-market-lab 的派生桥接使用仓库 checkout 的 `.venv` 与 `PYTHONPATH` | `/home/quant/repos/macro-market-lab/scripts/marketlab-maintain-market-bars-data-center.sh:6-7,23` | 与 `docs/current-state.md:12` 的“生产进程不再引用任何仓库 checkout”冲突，接管派生调度时必须改为 release venv + deployment manifest |

因此当前缺少的是生产目标的持久化控制及调度闭环，以及支撑它的持久化基座（事务、索引、迁移、身份）。底层数据生产和有界维护能力可以继续使用。

## 3. 用户在 WebUI 中管理什么

### 3.1 新建生产任务

首版规定“一项任务 = 一个 provider 的一个 symbol + 一组生产输出”。支持批量选择品种，但确认时展开成多个独立任务，分别暂停、报错和调度；保存显式品种集合，registry 新增品种不会自动扩大生产范围。

创建向导包含四步：

1. **选择数据**：provider、已批准 symbol、原始数据周期和价格语义。Dukascopy 原始层限定 1m BID。选择尚未注册的品种时，说明需要补 instrument/capability/session 配置，不让用户自由输入后直接启用。
2. **选择产物与范围**：只维护 provider_bars，或同时维护指定 market_bars 周期；历史起点或“从当前开始”；持续增量维护或一次性固定区间。首版直接展示已有 5m/15m/30m/1h/4h/1d/1w/1mo recipes 的可用性；没有注册的 market_bars 1m 复制 recipe 不作为已支持选项。
3. **选择计划**：手动、指定时间一次、每 N 分钟/小时、每天指定时间；周期任务可指定首次启动时间。首次启动时间是运行时间，历史起点是数据时间，使用两个清晰的表单字段。
4. **预览并保存**：显示可计算的未来 5 次计划、下一轮预计窗口、上游依赖、policy、初始补齐规模、容量是否允许及重复生产冲突。manual 无自动时间，once 只有一次；fixed_delay 展示相对完成时间的规则，不能把未知执行耗时换成确定日期。一次性任务可“保存并立即执行”，周期任务可“保存并启用”或“保存为暂停”。

选择 1w/1mo 输出时，向导显示并纳入必要的 1d 中间产物和成本。默认首版不开放 recipe 代码编辑；只选择已注册且版本固定的 canonical recipes。

产品示例：`EURUSD 持续生产`，provider=Dukascopy，原始层=1m BID，输出=5m/1h/1d，数据从指定历史日期开始，首次运行时间由用户设置，之后每 15 分钟检查更新。用户暂停后看到“暂停中：正在完成当前 60 分钟数据窗口”，随后显示已完成和待处理范围；继续后从未完成范围推进。

### 3.2 任务中心与统一调度视图

沿用 Maintenance 工作区，增加“生产任务 / 单次维护”两个入口，原有手工 backfill、quality、parity 保留。生产任务列表显示：名称、provider/symbol、产物、启用状态、本轮进度、数据时效、下次时间、最近结果、阻塞原因和操作。

任务详情包括配置及版本、依赖图、数据进度、执行历史、关联 run/manifest/finding、变更审计。进度使用“完成窗口数 / 当前已规划窗口数”和每个产物的完整边界；存在持续增长或尚未展开的 backlog 时，不显示虚假的总完成百分比。

统一调度视图放在任务中心，展示 scheduler 心跳、是否允许派发、执行积压、最老到期延迟、待补数据跨度、容量阻塞、按 provider 的退避、正在占用的任务和 worker。Operations 保留系统健康入口，并链接到这份视图。

支持：暂停、继续、立即执行、重试失败步骤、编辑未来配置、复制、归档、删除。暂停任务的“立即执行”返回明确冲突并提示先继续；如果已有执行尚未完成，“立即执行”定位到该执行，不再新建一轮。归档要求没有非终态 execution；对处于 enabled 的计划，归档必须在同一次操作内先暂停再归档，不得让在途执行失去归属。归档只停止任务使用，保留历史与数据；删除的语义与前置条件见 5.4。编辑配置不改变 `desired_state`：编辑一个暂停中的任务不会顺带恢复生产，恢复必须是一次显式动作。

全局暂停作用于新 scheduler 管理的全部生产任务，采用窗口边界暂停；保持其他手动维护的既有语义，并在界面明确范围。全局继续不能解除用户单独暂停的任务。完整停机仍由运维流程处理。

### 3.3 所有权与目录同步

一个“生产输出所有权键”（ownership key）标识一份数据只有一个生产计划负责：

```text
provider_bars / market_bars: (dataset_id, provider, symbol, timeframe, price_basis)
economic_observations:       (dataset_id, series_id)
```

- 同一所有权键在 `enabled` 与 `paused` 计划之间唯一；创建、复制、编辑与恢复都必须先通过冲突检查，冲突返回稳定错误码，不允许静默并存两个写入者。
- 归档与删除释放所有权；释放后其他计划可以接管同一键，历史归属不变。
- provider 或 symbol 变更不修改既有计划，而是复制成新的所有权键（见 5.3 末）。
- 目录同步：以 registry（approved instruments、recipes、maintenance policies）为权威，持续给出“已注册 × 已计划”的矩阵，并区分四种状态：**已计划 / 未计划 / 不可用**（未批准品种、recipe 不适用于该 provider 或 timeframe）/ **已计划但配置漂移**（见 5.3 的版本校验）。矩阵只读，不自动扩大生产范围。

### 3.4 统一调度视图的清单范围

统一调度视图同时展示两类对象，并在界面上明确区分：

1. **生产计划**（本文管理）：所有权键、产物、范围、节奏、状态、健康度、下次时间、进度与阻塞原因。
2. **治理型单元**（只读清单，不接管）：`market-data-center-monitor`、`market-data-center-smoke`、`market-data-center-provider-acceptance`、backup/restore 与 release 相关定时任务。每条显示所有者、触发节奏、最近一次 receipt 与结果。该清单只读：调度器不得启停、改写或重排这些单元；它存在的目的是让“统一视图”不谎称自己管理了主机上的全部计划。

清单是**观测投影，不是配置来源**：它不持久化定时任务的定义、不参与 tick 决策、不因清单读取失败而阻塞派发。它必须同时暴露“仓库声明但主机未安装”（例如 macro-market-lab 模板中存在、主机上不存在的经济与事件日历 timer）与“主机已安装但仓库声明中没有”的差异，并标明证据来源（`systemctl --user show` 与 receipt 索引）。差异只报告，不自动对齐。

## 4. 领域模型与状态

```text
ProductionTask（长期生产定义，含不可变配置版本）
  └─ TaskExecution（一次计划或手动触发）
       ├─ Step：原始数据窗口 → 一个或多个 Run 尝试
       ├─ Step：5m 派生窗口 → Run
       └─ Step：1d 派生窗口 → Run → 后续 1w / 1mo Step
```

| 对象 | 主要字段与约束 |
| --- | --- |
| ProductionTask | UUID task_id、alias（迁移自旧 task_id 的兼容引用）、name、definition_version、provider/symbol、outputs、window_policy、schedule、desired_state、created_by/updated_by；name 不作为身份或匹配键 |
| PlanOwnership | 所有权键（见 3.3）与 task_id 的唯一对应；在同一时刻只允许一个非归档计划持有；归档与删除释放 |
| TaskExecution | execution_id、task_id、definition_version、trigger_source、scheduled_for、enqueued_at、effective_range、state、outcome、coalesced_count、started/finished_at、safe error；冻结本轮配置和时钟基准，enqueued_at 为首批入队时间，零作业执行为空 |
| Step | step_id、execution_id、stage/recipe、半开数据窗口、依赖、输入 snapshot 引用、state、block_reason、attempt 关联、publication 结果 |
| Run | 沿用现有 run_id/status/receipt；增加可查询的 task/execution/step 关联；原 terminal receipt 不改写 |
| TaskProgress | per-output frontier、已完成区间引用、未解决 gap、catch-up 区间、待重算区间、已处理 publication 游标；以发布和 coverage 为依据，可核对和恢复 |
| SchedulerState | desired dispatch 状态、心跳、instance/lease/fencing token、最近调度与恢复错误 |

任务的 `desired_state=enabled|paused|archived` 表示用户意图；`phase=initializing|catching_up|maintaining` 表示数据阶段；`health=healthy|lagging|blocked|attention` 是读模型，不能反向替用户启停任务。

执行状态采用 `pending → running → completed`，另有 `running → pausing → paused → running`；没有在途窗口的 pending 可以直接进入 paused，零工作量的 pending 可以直接 completed/skipped。仅 pending 或未完成部分可以等待依赖、容量、重试或资源；用独立 `block_reason` 表达，不把等待误报为失败。completed 必须携带 `outcome=pass|degraded|failed|skipped`，失败历史不因后来成功而重写。

源数据已确认 gap 可以让本轮 completed/degraded，并把缺口及其受阻的输出范围留在 TaskProgress；不让一轮执行永久等待供应商。尚未闭合的 bucket 留作后续待处理，不制造 failed run。结构性质量错误结束相应步骤并使本轮 failed；不可恢复的配置错误也以 failed 结束本轮，并阻止新配置版本生效前的后续派发，不能无限重试。

新定义使用独立 `production_*` 表（计划、配置版本、execution、step、progress、输入引用、所有权、调度状态）；现有 `maintenance_tasks` 继续承载单次维护的兼容记录。两者身份明确区分，不给现有 `/maintenance/tasks` POST 暗中增加“创建后长期自动执行”的副作用；接入时必须修改的既有结构（归属列、到期索引、部署身份表清单、迁移机制）见 7.4。

## 5. 时间计划与暂停继续的规则

### 5.1 支持的计划

| 类型 | 明确语义 |
| --- | --- |
| manual | 仅用户立即执行；next_run_at 为空 |
| once | 在 run_at 接受一次执行；重启后过期则补一次并记录 late_by，完成后没有下次时间 |
| interval / fixed_rate | 按 anchor + N × interval 对齐；默认用于新建周期任务，执行耗时不会改变锚点 |
| interval / fixed_delay | 本轮完成后等待 interval；用于兼容旧 timer 的“结束后 15 分钟”语义，运行中展示“完成后 15 分钟”而非伪造具体时间 |
| calendar / daily | 在所选 IANA 时区每日 local_time 执行；hourly 是 interval=60m 的表单快捷项 |

所有执行时刻以 UTC 存储和比较。calendar 额外保存 timezone 与 local_time 作为计划定义；全局 UI 时间显示模式只改变显示，不修改计划语义。夏令时缺失的本地时刻顺延到当日首个合法时刻，重复时刻只执行第一次；预览必须显示实际 UTC 和 offset。

无时区日期时间、过去的首次启动时间、非正周期和过短周期在创建时拒绝，并给出字段级错误；恢复已有过期计划属于 misfire，不等同于创建过期计划。首版最小周期为 5 分钟，新建周期任务默认 fixed_rate 15 分钟；provider 可用延迟独立生效。旧 timer 导入必须保留其 `fixed_delay=15m` 语义（见第 9 节），不得静默改成 fixed_rate；两种类型的差别必须在向导中一句话说明，并在计划详情里显示“下一次确切时刻”或“完成后 N 分钟”，不得用相对语义伪造确定时间。若托管运行数据显示单轮耗时经常超过所选周期（表现为持续的合并触发），允许把该计划改为 `fixed_delay`；这是计划字段，不是架构决策。

### 5.2 到期、错过周期与重叠

- 同一任务最多一个非终态 execution。定时和手动触发都经过这一约束。
- fixed_rate/daily 错过多次触发时，默认 `coalesce_latest`：合并成一轮检查，记录合并次数及首末计划时刻，下一时间回到未来锚点。不会为停机期间的每个 tick 批量创建 run。
- **合并触发次数不等于丢弃数据范围**。TaskProgress 保留停机前到当前的待补区间，按预算分片补齐；若跨度超过容量允许的无人值守范围，显示 backlog_blocked，不能截成最近 2 天后假称追平。
- fixed_delay 从本轮终态时间推进。失败也进入策略退避；manual/once 的失败不会凭空变成新周期任务。原执行未终结时按 worker 策略重试步骤；执行已终结后，人工重试创建带 retry_of_execution_id 的新执行，只规划仍未完成的数据需求，保留原执行结果。
- 任务忙、用户暂停、全局暂停、容量保护或 provider 退避期间不堆积无界 execution；保留最早待处理数据时间和合并触发摘要。

### 5.3 暂停与继续

首版采用“窗口边界暂停”，其行为必须满足：

1. 暂停操作在事务中记录 desired_state=paused；此后 scheduler 不提交新步骤，worker 不领取该 task 的 queued 作业，包括自动 retry 和派生作业。
2. 已领取窗口允许结束当前执行尝试及必要的 publication/ledger 收口；有在途窗口时显示 pausing，无在途窗口后显示 paused。已有 worker 超时规则继续有效，不承诺立即停止网络请求。
3. 未领取作业保持可恢复的等待状态；暂停不增加 attempt_count，不视为失败。
4. 继续先恢复已存在 execution 的未完成步骤，复用已完成窗口和已固定的有效输入；随后按 misfire 与 catch-up 规则处理新到期需求。
5. pause 与 claim 竞争必须有事务先后关系：pause 先提交则不允许新 claim；claim 先提交则该窗口视为在途，界面显示 pausing。

任务编辑使用 definition_version 乐观锁。名称等展示信息可即时更新；schedule/outputs/window policy 形成新版本，仅影响后续 execution。已有 paused execution 继续使用原版本。provider/symbol 变更通过复制新任务完成，避免旧进度绑定到不同数据。

配置版本必须持久化解析当时得到的配置摘要（dataset、capability、instrument、session、calendar、quality profile、maintenance policy 的 digest，由现有 planner 产出）。每次 reconciliation 比对当前 registry：摘要变化意味着 recipe、品种元数据或治理参数在计划之外被改动，此时该计划进入 `health=config_drift`、停止新派发、保留已发布数据与在途收口，并要求显式确认（`acknowledge_drift`）后才能恢复派发；已处于 enabled 的计划在漂移后也必须先确认，不得靠重新启停绕过。不得静默按新语义继续生产。

### 5.4 归档与删除

| 动作 | 语义 | 前置条件 | 保留 |
| --- | --- | --- | --- |
| archive | 停止使用，可恢复 | 无非终态 execution（enabled 计划在同一次操作内先暂停再归档） | 计划定义、执行历史、数据 |
| delete | 删除计划**定义**（墓碑） | 已 archive 或 paused；无非终态 execution；所有权已释放 | canonical part、manifest、run、terminal receipt、`write_audit`、墓碑行 |

规则：

1. 删除只作用于计划定义，永不删除数据与历史；不存在“删除计划时顺便清理数据”的产品动作。容量不足只能走既有容量治理与保留策略。
2. 删除必须在同一事务内释放所有权、写墓碑（`deleted_at`、definition 摘要的 sha256）并记录写审计（actor、时间、计划身份、结果）；被拒绝的删除尝试同样入审计。
3. 墓碑保留 alias 解析：旧 task_id 与旧链接必须仍能查询到“该计划已于某时删除”，返回明确状态而不是 404 或无解释的空结果。
4. 已完成 execution 与 run 的归属不因删除改变；查询历史仍能追溯到已删除的计划身份。

## 6. 原始层到派生层如何持续推进

### 6.1 窗口策略

持续生产在接受一轮执行时冻结 `planning_at`，据 provider policy 计算 `effective_end = closed_boundary(planning_at - availability_lag)`，再结合任务历史起点、已发布 coverage、进度和尾部重查范围规划。

Dukascopy 默认继承 180 分钟 availability lag、2 天尾部复查、60 分钟分片及 180 分钟 gap cooldown。每 15 分钟运行的计划只控制检查频率；数据新鲜度必须同时展示 provider 延迟、实际观测边界和完整边界。

首次补齐和长停机 catch-up 使用持久化 backlog。每次只展开有预算的窗口；近期 tail 及其可用派生优先，其余预算推进历史补齐和 cooldown 到期的 gap。不能让单个永久缺口阻止新 tail，也不能通过使用 max_ts 代替完整 coverage 消除旧缺口。

容量判断同时检查任务总体无人值守补齐跨度和本批数据窗口；超过 31 天的 backfill 不能靠拆成 1 小时请求绕过 warning 门禁。critical 时拒绝新发布型任务派发，保留 backlog 与读路径；采用有效 Settings policy，不把历史文档中的某组生产阈值写死。

一次性维护使用固定 `[start,end)`；持续维护用动态窗口策略。两者在模型和界面上必须可区分。

### 6.2 依赖与数据就绪

```text
定时到期 / 继续 / 立即执行
           ↓
coverage + policy + progress → 原始窗口计划
           ↓
provider_bars 1m 发布并核对 coverage
           ├─ 完整 5m/15m/30m/1h/4h bucket → 对应 market_bars
           └─ 完整 1d bucket → market_bars 1d → 完整 1w / 1mo bucket
```

每个下游窗口同时满足：上游 publication 已完成、对应范围 ready、bucket 已按 session/calendar 闭合、recipe/config/version 已解析。前一 run 为 pass 只是必要信息，不能代替具体范围 readiness。

使用 registry 的明确 recipe 依赖做拓扑排序和环检测，自动补齐 1d 等中间依赖；不依赖字符串排序来表达执行关系。只展开用户选择产物所需的图，不自动运行全部 recipes。

一个 symbol 的某个 1h bucket 缺 1m 数据，只阻塞覆盖该缺口的输出 bucket；其他完整 5m bucket 和其他 symbol 仍可推进。gap repair 成功后，将其受影响的目标 bucket 加入持久化重算集合，逐层更新派生 lineage。

raw 阶段没有新窗口但 derived 尚未产出时，仍应规划派生。手动修复或其他受治理入口发布的新 raw 数据也必须可发现：周期性 reconciliation 根据持久化 publication 游标补记受影响范围；publish 到一半进程退出后，manifest/ledger 恢复与 reconciliation 能补上后续派生，不能只依赖进程内事件。

### 6.3 输入 snapshot 与计算幂等

持久化 Step 的输入 manifest/part 引用、校验信息、recipe/version 和 config digests，得到可重建的执行输入。run 在提交时携带这一固定输入的引用，重启和排队后的执行读取被固定的输入；新增 canonical part 不改变已经接受的输入。查询 cursor 的进程内 snapshot 缓存不作为执行恢复依据。

存储输入引用时使用共享、可分页读取的快照记录，避免把同一大 manifest 列表复制到每个 run 或 WebUI 响应。缺失或校验失败时停止该步骤并报告 input_unavailable，不能偷偷改用 current snapshot。

执行侧必须能从持久化的 part 引用集合（相对路径、产出该 part 的 run_id、schema 版本）重建被固定的输入，而不是执行时重新解析当前 catalog 再与提交时的 snapshot_id 比较。现有实现属于后者，且比较失败会被判为不可重试错误，使 run 永久失败（`backend/src/data_center/ingest/process.py:35,55-61`）；在原始层每 15 分钟推进一次的生产节奏下，排队中的派生工作必然与发布竞争。因此：新增 canonical part 时，已接受的执行按既定输入完成，新数据只进入待重算集合，不得让既有执行失败。

新发布的数据影响已有输出时生成新的重算请求；原 execution 的成功是“针对当时固定输入完成”，并不声称输出永远最新。由待重算范围及 per-output freshness 明确显示差异。

幂等至少分三层：计划触发键去重、Step 到 Run 的提交去重、同 recipe/config/输入/窗口的物化检查。不同任务需要共享产物时，首版对相同输出 selector 的持续生产所有权做冲突检查，并对写入 selector 的活动窗口串行化；不同 recipe/version 的输出按其真实 identity 处理。旧手动入口也必须遵守活动 selector 的领取约束。

首版可以保守沿用同完整 snapshot 的物化判断；snapshot 变化导致的额外重算必须有预算，不能每 15 分钟全历史重算。后续再按受影响窗口的输入内容缩小 fingerprint，不能直接改变现有 public snapshot_id 契约。

## 7. 调度器的实现位置与可靠性

### 7.1 一个生产任务模块，两个调用入口

任务版本、调度、进度、幂等、暂停和历史聚合必须集中在 production task module。WebUI 对应的 HTTP adapter 和 scheduler 进程跨同一 interface，不各自拼 job payload 或解释 policy。

模块 interface 为：`preview(definition)`、`create(definition)`、`change(task_id, command, expected_version)`、`read(query)`、`reconcile(now, budget)`。clock、ledger、catalog/policy 作为明确依赖注入；不为只有一种实现的存储提前搭建插件框架。

原始窗口 planner 与派生 planner 是内部 seam，复用现有 `platform`、`maintenance_runner`、`derived_maintenance_runner` 的有效规则。重构后原 CLI 成为同一任务模块的 adapter，避免长期保留独立循环和另一套调度真相。

新增独立 `data_center.scheduler_main` 与 `market-data-center-scheduler.service`。systemd 负责守护进程和重启；具体品种与启动计划来自 ledger。scheduler 不嵌入 API 请求线程，不运行 provider fetch，也不等待某个 run 几十分钟完成；通过短周期 reconciliation 推进。

### 7.2 持久化与原子提交

在现有 SQLite ledger 同库增量添加生产任务、配置版本、execution、step、run 关联、progress、输入 snapshot 引用和 scheduler state。关键字段独立列与索引，详细参数可为 JSON；不要通过每次全量反序列化历史 runs 找到到期任务。

定时触发的唯一键为 `(task_id, schedule_revision, scheduled_for_utc)`，其中 schedule_revision 仅在计划语义变更时增加；手动触发使用 `(task_id, idempotency_key)`。另设每 task 最多一个非终态 execution 的唯一约束，不能只依靠应用先查后写。Step 用持久化 ID 和唯一的执行内窗口/recipe/输入身份关联，恢复规划不得通过重新生成随机 ID 绕过去重。

调度事务与恢复必须满足以下顺序：

1. 按 next_run_at/next_attempt_at 索引、有限批量读取 enabled 任务。事务内创建或复用唯一 execution，领取带 lease 与递增 fencing token 的规划权；同一 task 最多一个非终态 execution。
2. 事务外读取 bounded coverage、容量和配置并生成有界计划。不得持有 SQLite 写锁进行 catalog 扫描、网络调用或等待 worker。
3. 短事务内重新验证 desired_state、global pause、execution 固定的 definition_version、lease/token。任务已有新版本不改变旧 execution 的版本；本批规划必须与该 execution 的固定版本一致。保存计划、step，并将本批 run、jobs、关联和 audit 一起提交；同 `(step_id, submission_generation)` 只能出现一个初始 run。
4. 接受本轮计划及首次提交的同一事务中，fixed_rate/daily 推进 next_run_at，once 清除 next_run_at；零工作量则原子记录 skipped 后作相应处理。fixed_delay 在本轮终态时计算下次时间。规划或容量失败不伪装成 enqueue 成功，保留 execution、错误和 next_attempt_at。
5. 后续 tick 根据 Run/manifest 收口 Step、解除依赖，并按预算派发下一批。所有步骤终结后记录 execution outcome、更新 progress 和必要的 fixed_delay 时间。

现有 `enqueue_job()` 每次新建连接和随机 run_id，`enqueue_ingest_plan()` 逐个调用它，不能直接包在外层事务中假装原子。需要 ledger 内部支持同一连接的批量幂等插入，并覆盖“部分窗口插入时崩溃”和“事务成功但响应丢失”的故障点。

lease 到期只允许其他 scheduler 接管规划与 reconciliation，不能直接重置 worker 的 running run。旧 owner 必须携带 token 才能提交；失去 lease 后迟到的结果被拒绝。任务暂停、lease 过期及进程切换后都可以依据同一个 execution 恢复。

承诺是每个计划时隙只接受一次逻辑执行、每个步骤幂等提交。网络重试和 worker 恢复可能多次尝试同一窗口，不能宣称外部 fetch 严格 exactly-once；canonical 发布继续依靠现有 staging、hash 校验和不可变 receipt 保证。

### 7.3 预算、重试与可观测性

首版保留每 ledger 一个实际数据 worker。scheduler 可竞争或故障接管，但不把这一点表述成多 worker 并行生产。每任务最多一个已派发未终结步骤；tick 间隔、单次任务扫描数和全局预取量必须有可配置上限，并展示有效值。初始调优参数记录在实施计划，调整不得突破本规范的公平性、有界查询和暂停语义。

串行容量已按实测规模核算（8 品种 × 31 天 × 60 分钟分片约 5,950 个窗口；按每窗口 3–10 秒估算，一次性历史回补约 5–17 小时），因此首版可以单 worker 交付，但**并行度必须是策略字段而不是写死的假设**：每个计划与全局各有一个并发上限，默认值为 1，取值变化只影响派发数量，不改变本节的公平性、有界查询与暂停语义。引入大于 1 的并发之前，必须先完成 WAL 与写事务压测、每窗口幂等证据与 provider 退避验证；重新评估的触发条件是“需要多年历史”“需要同时维护多个高周期产物”或“灰度证明串行无法满足新鲜度”。

任务之间按最老到期顺序轮转，近期生产使用优先队列但保留历史补齐配额，防止某个大 backfill 或频繁任务占满唯一 worker。provider 限流/退避状态持久化；失败任务的冷却期间允许其他任务前进。

底层瞬态网络重试继续使用 worker 的有界 attempts/backoff。scheduler 不对仍在 worker retry 的 run 另建副本；耗尽后保留终态并按明确策略决定下一次补齐。人工 retry 创建新 run 并保留 retry_of；原 execution 未终结时关联原 Step，已终结时归属后续 retry execution 的新 Step。不得绕过 paused、容量和 selector 约束；现有 `/runs/{id}/retry` 遇到生产任务归属时也进入同一管理逻辑。gap cooldown 与网络重试分开。

展示和记录：scheduler heartbeat/identity、tick duration、due lag、coalesced triggers、backlog age/range、queued/running/paused counts、provider backoff、capacity blocked、dependency blocked、latest successful execution、per-output freshness 和 lease 接管次数。内部错误不包含凭据或 provider 请求敏感信息。

readiness 分开报告读可用、写可用、worker 状态和 scheduler 状态。scheduler 停止不能导致已有数据不可查询，也不能被整体绿色状态遮蔽。所有 task/global 管理动作复用现有身份、请求 envelope 和 write audit，记录 actor/source/version；后台使用明确的 system actor，不保存用户 session cookie。

### 7.4 基座前置条件（不满足则无法验收）

调度器与 API、worker、monitor 共用同一个 SQLite ledger。以下各项是本功能的前置条件，必须在功能验收前完成并单独验证：

1. **并发写入**：显式启用 WAL 与有界的 busy 处理，写事务保持在毫秒级；不得在持有写锁时读取 parquet、访问网络或等待 worker。需要多进程争用测试与压测证据（AC14、AC22）。
2. **到期扫描索引**：为任务到期、execution 状态与 `jobs` 的领取条件建立索引；现有领取路径把全部 queued 行读入应用层，且暂停判断依赖 `job_id` 字符串前缀匹配，必须改为按归属字段（计划/execution/step 标识列）判断。
3. **批量原子提交**：提供同一连接、同一事务内的批量入队；“接受一轮执行”必须把计划、步骤、run、job、归属、进度和审计一起提交或一起回滚。现有逐窗口 `enqueue_job()` 之后再单独 upsert 任务与写审计不构成原子操作。
4. **迁移机制**：引入可重复、可校验的顺序迁移与 schema 版本记录；新表与新列必须能从当前生产库（无版本记录）升级，并有备份/恢复回归。DDL 不得继续分散在各方法体内由每个进程重复执行。
5. **SQL 侧读模型**：计划历史与执行查询需要 `runs` 具备可索引的列（计划、execution、step、状态、时间），不能建立在“读取全部 runs 再在内存过滤”的读模型上。
6. **注入时钟**：计划时间、misfire、heartbeat 与退避必须使用可注入时钟，测试不得依赖真实等待。
7. **部署身份与证据**：新增调度状态表必须进入 deployment 的逻辑哈希表清单；scheduler 服务必须加入 deployment 的 `service_names`，使 activation/rollback 会重启并校验它；调度 receipt 的 action 必须登记到运维 receipt 视图的 action 列表，否则在运维页面不可见。
8. **所有权与暂停的落库点**：所有权唯一性与“暂停后不可领取”必须由数据库约束与查询条件保证，不能只靠应用先查后写；暂停与 claim 的竞争顺序由事务提交顺序决定（5.3 第 5 条）。

## 8. HTTP 契约

全部使用既有 `/api/v1` envelope、身份校验和稳定错误码；复用当前认证机制，不借此功能新增角色体系。

| 路径 | 用途 |
| --- | --- |
| `POST /production/plans` | 无写入预览；计划时间、依赖、窗口估计、冲突与容量 |
| `POST /production/tasks` | 创建定义，支持 Idempotency-Key；重复相同 key 返回同一结果，不同内容返回冲突 |
| `GET /production/tasks` | 按 provider/symbol/state/health 筛选及 cursor 分页 |
| `GET /production/tasks/{id}` | 配置、进度、当前执行、下一计划与健康投影 |
| `PATCH /production/tasks/{id}` | 带 expected_version 修改配置；版本冲突为 409 |
| `POST /production/tasks/{id}/actions` | 有限 command：pause、resume、run_now、archive；带幂等键与版本 |
| `GET /production/tasks/{id}/executions` | 分页历史，包含 trigger_source 与配置版本 |
| `GET /production/executions/{id}` | 本轮概况，不无界展开全部步骤与 run |
| `GET /production/executions/{id}/steps` | 步骤、依赖、窗口、阻塞与关联 run，cursor 分页 |
| `POST /production/executions/{id}/retry` | 为已终结执行的失败需求创建关联的后续执行，支持幂等；存在其他活动执行时返回 409 |
| `GET /operations/scheduler` | 调度状态、预算、阻塞统计与系统身份 |
| `POST /operations/scheduler/actions` | 全局 pause_dispatch / resume_dispatch，记录审计 |

`/runs` 与详情投影增补 task_id/execution_id/step_id 的查询关系。terminal receipts 通过独立关联读模型增强，不原地修改旧 JSON。`/capabilities` 增补可用任务类型、计划类型、最小周期、生产输出、计划 `health` 与 `block_reason` 枚举及 scheduler 是否启用；前端依实际能力显示。

管理动作沿用既有约定：`POST /production/tasks/{id}/actions` 的 command 集合为 `update | pause | resume | run_now | retry | archive | copy | delete | acknowledge_drift`（启停由 `resume`/`pause` 表达，不另设 enable/disable；`copy` 对应 3.2 的复制），全部支持幂等键与 `expected_version`；删除按 5.4 的规则返回墓碑结果。列表与历史复用既有 envelope、错误码与 HMAC cursor 约定（cursor 绑定筛选条件集，越界返回 422）。

实施时必须同时满足仓库既有的强制门禁：任何新增写路由都要登记进 `backend/tests/test_api_surface_contract.py` 的 `MUTATING_ROUTES`（含鉴权与审计决策），新增 systemd 单元的 `EnvironmentFile` 必须位于机器级配置目录下（`scripts/production_env_check.py` 校验），否则统一门禁直接失败。

## 9. 接管、兼容与回滚要求

旧单次维护请求和任务记录保持兼容；新增生产任务使用独立的持久化定义。旧手动任务不得被自动赋予周期计划。schema migration 必须可重复、保留旧数据，并通过包含 ledger 与固定输入引用的备份恢复验收。

新增 scheduler 默认关闭；导入旧生产配置时创建 paused 任务，保持实际 symbol allowlist、recipe/version、数据范围、run_scope 与有效 policy。原始层旧 timer 的语义为 fixed_delay=15m，导入不得静默改为 fixed_rate。用户新建任务可明确选择保存并启用。

接管范围必须盘点 Data Center 原始层 timer，以及运维记录中的 macro-market-lab 派生入口。新旧入口不能同时拥有同一生产 selector；切换必须先阻止旧入口新提交、收口在途窗口并核对 ledger，然后启用新任务。未验收的品种和周期不得随迁移扩大。

接管清单必须逐条覆盖实测事实，而不是只停本仓库的 timer：

1. **原始层**：`market-data-center-1m-maintenance`（启动后 5 分钟 + 上轮结束后 15 分钟；导入为 `fixed_delay=15m`，不得静默改成 fixed_rate）。
2. **派生层**：macro-market-lab 的 `marketlab-market-bars-maintenance`（每天 06:30 UTC，调用 Data Center runner，当前只生产 5m）。首版接管只覆盖它实际生产的产物；其它 recipe 按需创建。接管时该入口必须改为使用 release venv 与 deployment manifest，禁止继续引用仓库 checkout。
3. **先行独立修复（不属于本功能交付，但必须在接管前完成）**：`market-data-center-provider-acceptance` 的 `ExecStartPost` 承担了 `operations retention-audit`；该验收自 2026-09-11 起连续失败，保留审计随之停跑。必须把保留审计拆成独立单元（自己的 service/timer，具备自己的 `EnvironmentFile`、证据目录与 receipt），使其不依赖验收结果。它以独立的小发布交付并留痕，不得并入调度器接管批次，也不得因为“接管时会一起处理”而继续推迟；provider 验收本身的失败按独立缺陷处理，不与本解耦混为一谈。新的保留审计单元必须有自己的 receipt action，并登记到运维 receipt 视图的 action 列表，否则运维页面看不到它。
4. **只读清单**：monitor、smoke、provider acceptance、backup/restore 与 release 定时任务不接管，但按 3.4 进入统一视图的清单，显示所有者、节奏与最近 receipt。
5. **仓库模板与主机现实**：以主机实际安装的单元为准（例如 macro-market-lab 仓库模板中的经济/事件日历 timer 并未安装），差异必须记录在接管 receipt 中，不得据仓库文件推断生产现状。

scheduler 必须纳入 immutable deployment 的身份、启动、健康检查与回滚管理；systemd 仅负责守护进程，业务计划来自任务 registry。部署后 WebUI、API、worker、scheduler 的身份必须与相应 release receipt 一致。

回滚必须先阻止新 scheduler 的派发并收口在途窗口，保留任务表、输入引用和历史证据，再按已记录配置恢复旧入口。旧 binary 不识别新任务归属时，必须通过已验证的兼容处理防止其领取暂停的 queued 作业。不得恢复旧 ledger 覆盖切换后产生的历史。

切换与回滚须保留 task/execution/run/manifest 关联及 receipt。生产接管必须覆盖至少一个 crypto 和一个 FX 品种，再扩展到原有批准范围；不能以自动化测试通过替代真实接管证据。涉及外部项目入口与生产激活的操作遵循仓库既有合并、发布及运维要求，参见[发布清单](../release-checklist.md)和[运行手册](../operations-runbook.md)。

## 10. 不变量

1. 任务身份使用稳定 ID；任务是否启用、执行状态、数据是否完整分别记录，不靠名称或状态混用推断。
2. 一项任务最多一个非终态 execution；每个计划时隙和步骤的提交必须幂等。
3. 暂停先提交则不能产生新的任务派发或 worker claim；已领取窗口允许安全收口。全局继续不能解除单独暂停。
4. 已发布窗口不因暂停、重启或触发合并而重复生产；错过的触发可以合并，待补数据范围不能丢弃。
5. 下游读取固定、可恢复且校验通过的输入；具体范围 ready 且 bucket 完整才可发布。缺口不得用 synthetic bar 隐藏。
6. lease 接管不得改写 worker running run；终态 run 和 execution 的结果不因重试成功而覆盖。
7. scheduler 不绕过容量、身份、审计或现有 worker 发布路径；大回补不能通过小分片绕过总体门禁。
8. 查询、调度扫描、预取和重算必须有界；单个失败任务或大 backfill 不得长期饿死其他任务。
9. 旧 timer 与新 scheduler 不得双重生产；回滚不能让暂停作业被旧 worker 意外领取。
10. 功能实现、隔离验收和生产接管分别记录进度；未完成接管不得宣称已经统一管理现有生产。
11. 同一所有权键在任何时刻最多只有一个非归档计划；归档与删除必须释放所有权，释放后历史归属不变。
12. 删除只删除计划定义：canonical 数据、manifest、terminal receipt、审计与已完成执行的归属永久保留。
13. 计划的配置版本与解析时的配置摘要一起持久化；registry 漂移时停止派发并要求显式确认，不静默按新语义生产。

## 11. 验收标准

以下编号用于实现、测试和评审追踪；实施计划只能引用，不另行定义验收语义。

| 编号 | 必须验证的结果 |
| --- | --- |
| AC01 创建与能力 | WebUI 创建一个品种及指定产物的任务；批量创建生成独立任务。未批准品种、无效 recipe、重复输出所有权被明确拒绝；可用能力由后端提供 |
| AC02 计划时间 | 首次启动前不 enqueue；manual 不自动触发；once 只接受一次；fixed_rate 按锚点、fixed_delay 按完成时间、daily 按时区执行，预览与实际相符 |
| AC03 时区与错过周期 | daily 的 DST 缺失/重复时刻、全局显示模式切换、执行耗时超过周期、重启过期计划都遵守第 5 节；合并触发保留来源和次数 |
| AC04 并发与 fencing | 两个 scheduler 竞争只接受一个 execution；lease 到期可以接管，旧 owner 迟到提交被拒绝 |
| AC05 原子提交 | 批量分片入队中途失败全部回滚；事务成功但响应丢失后重试仍关联同一批 run；计划推进与接受执行保持一致 |
| AC06 暂停继续 | 验证 pause/claim 两种顺序、窗口写入中暂停、queued/自动 retry/派生阻止领取、重启后继续、完成窗口不重跑；名称相近任务互不影响 |
| AC07 全局控制与版本 | 全局继续不解除单独暂停；暂停任务立即执行被拒绝；活动执行不会被手动重复触发；配置并发编辑返回 409，已有执行保持原版本 |
| AC08 数据补齐与容量 | 首次历史补齐、停机超过 2 天及 31 天都保留 backlog；warning 时不能通过分片绕过总体 backfill 门禁，critical 时停止新发布型派发 |
| AC09 自动生产依赖 | 一个任务经真实隔离 worker 产出 raw 与所选 derived 数据并可查询；周/月输出等待日线依赖，合法闭市和未闭合 bucket 不制造虚假失败 |
| AC10 publication 恢复 | raw 发布后 scheduler 崩溃，重启补上 derive；raw 无需更新但派生缺失仍会补产；外部受治理手工修复触发下游重算不遗漏 |
| AC11 固定输入 | enqueue 后新增 raw part、snapshot 缓存到期或进程重启，原 derive 仍使用可重建输入；后续变化进入重算集合，输入缺失/hash 错误失败关闭 |
| AC12 缺口与重算 | 缺口阻塞相应 1h bucket，其他完整 5m bucket 和其他品种可推进；gap cooldown 不制造反复失败，修复后受影响产物与 lineage 更新 |
| AC13 错误与重试 | 瞬态失败有界退避，不生成并行 retry 副本；结构性错误和配置错误可诊断；手动重试保留原 terminal 结果与关联并遵守暂停及容量约束 |
| AC14 公平性与规模 | 100 个暂停/到期混合任务及多年历史记录下分页、扫描和预取有界；tick 的 DB 派发部分 P95 < 5 秒，coverage/网络不持写锁；持续失败或大 backfill 不饿死其他任务 |
| AC15 WebUI 闭环 | 在隔离浏览器走通创建、预览、定时启动、数据查询、暂停、重启、继续、历史/run 追踪；展示 next run、实际进度、数据时效及具体阻塞原因 |
| AC16 身份与审计 | 未授权写入拒绝；幂等键重放和版本冲突明确；用户与 system actor、scheduled_for/enqueued_at、task/execution/run、错误和操作审计可追溯 |
| AC17 升级与回滚 | schema migration 可重跑，备份恢复保留计划和输入；旧手动请求兼容；新旧 timer 切换无双写，回滚不领取暂停作业且历史与查询保持可用 |
| AC18 完整交付 | Python 3.10/3.11/3.12、Node 22、统一 gate、隔离浏览器/服务重启、fixture 或第二 provider 复用、生产原始/派生接管及回滚证据齐全 |
| AC19 所有权与冲突 | 同一所有权键的第二个计划被稳定拒绝；复制与恢复先做冲突检查；归档或删除释放后可接管；旧归属历史不变 |
| AC20 删除与墓碑 | 删除前置条件生效；所有权释放、墓碑与审计在同一事务；被拒删除同样入审计；alias 可解析到墓碑；canonical 数据、run、receipt 与历史归属不被删除或改写 |
| AC21 配置漂移 | 计划跨 release 后 registry 摘要变化即 `config_drift` 并停止派发，已发布数据与在途收口正常；确认后恢复；无漂移计划不受影响 |
| AC22 基座与迁移 | WAL 下多进程争用无 `database is locked` 失败；到期扫描有界且走索引；批量入队在中断与响应丢失下不产生重复 run；迁移可从现有生产库升级且可重跑；备份/恢复保留计划、输入引用与进度；deployment 逻辑哈希覆盖调度表 |
| AC23 目录矩阵与只读清单 | “已注册 × 已计划”矩阵正确区分已计划/未计划/不可用/漂移；治理型单元以只读清单显示所有者、节奏与最近 receipt，且无法通过调度器启停或改写 |
| AC24 部署身份与运维可见性 | scheduler 进入 deployment `service_names`，activation 与 rollback 会重启并校验其身份；readiness 区分 scheduler 状态；调度 receipt 出现在运维 receipt 视图；新增路由与单元通过仓库既有契约门禁 |

测试使用注入时钟、临时 SQLite、隔离 canonical/evidence/backup 与 fixture/provider mock。集成验收必须覆盖真实 worker 的 staging/publication/readback 行为，不仅验证 mock enqueue。统一门禁为 `bash scripts/ci.sh all`；CI 与浏览器验收不得读取生产数据或联系真实 provider。真实 provider、生产切换和回滚分别留存独立 receipts。

结构化证据须绑定 commit、环境、动作、起止时间、软件版本、结果与安全错误类别，并能关联本表编号。只有功能、隔离验收与原有生产入口接管全部完成，才可将本规范标记 complete。

## 12. 实施与修订记录

实施顺序、代码落点、阶段验证和切换步骤见[实施计划](../plans/2026-09-14-production-task-scheduler.md)。

2026-09-14：在原 Maintenance Scheduler 规范上扩展生产任务定义、指定首次启动与 interval/daily 计划、窗口级暂停继续、动态数据范围、原始/派生依赖、持久化输入、统一 WebUI、接管与回滚。原有幂等、恢复、UTC、容量和审计约束保留；“多 worker”明确为多个 scheduler 的可靠竞争，数据 worker 首版仍为单 supervisor。本次为规范修订，不代表功能已经上线。

2026-09-14（第二次修订，并入独立评审结论）：新增所有权键与目录同步（3.3）、统一视图的只读清单范围（3.4）、归档与删除语义（5.4）、配置漂移校验（5.3）、基座前置条件（7.4）、固定输入的可重建要求（6.3）、接管清单的实测条目与副作用解耦（9），以及 AC19–AC24；不变量增加第 11–13 条。三项已确认的决策：删除只删计划定义并保留数据与历史；接管首版只覆盖当前已有周期生产者的产物（原始 1m 与 1m→5m 派生），其余已注册 recipe 按需创建、不默认回补；治理型 timer 只进入只读清单、不被接管。评审证据与缺口编号见[调度能力分析与深化设计](../plans/2026-09-14-scheduler-capability-analysis-and-deep-design.md)。

AC19–AC24 已在同一次修订中映射到[实施计划](../plans/2026-09-14-production-task-scheduler.md)的阶段（S0–S5）；该计划此前只以未提交草稿形式存在，本修订是它第一次进入版本控制，因此以 S0–S5 为唯一阶段编号。

2026-09-14（第三次修订，决策定稿）：把维护者确认的四项决策写入本文——统一视图只读纳入治理型单元（§1 决策 1、3.4）、首版单数据 worker 但并行度作为策略字段（决策 2、7.3）、新建周期计划默认 fixed_rate 而旧 timer 导入保留 fixed_delay（决策 3、5.1）、`retention-audit` 解耦作为接管前的先行独立修复（决策 4、9.3）。同时明确只读清单是观测投影而非配置来源，须报告“仓库声明 vs 主机已安装”的差异。实施计划已把该先行修复列为接管前必须完成的独立工作项。
