# 调度能力分析与深化设计（独立评审）

日期：2026-09-14
状态：分析完成，设计提案待维护者决策（未实施、未激活任何生产动作）

关系说明：

- 本文是**独立评审 + 深化设计**，不替代 [生产任务与统一调度管理设计与验收规范](../specs/2026-09-14-maintenance-scheduler.md)（下称 spec）与 [实施计划](2026-09-14-production-task-scheduler.md)（下称 plan）。
- 第 2 节回答"这个仓库需要哪些调度能力"，第 3 节列出 spec 尚未覆盖或需要修订的点（G 编号），第 4 节给出可落地的模块接口与数据模型，第 8 节记录已确认决策与仍待确认项。
- 冲突处理建议：需求以 spec 为准；若采纳本文 G 编号中的条目，先修订 spec 再实施，避免两份文档各自表述。

调研基线（只读核对，未改动任何文件、未启停任何单元）：

- 源码：`main` @ `5333394`（= `origin/main`，工作区已有他人未提交的 spec/plan 改动）。
- 生产：deployment `3e2362ab0b31-6005b252`（`software_version=0.5.0`，`source_commit=3e2362a`），API/worker systemd active，`/health/ready` = `ready`、capacity `ok`、queue 0。
- 主机：`quant-server`；ledger `/home/quant/market_lake/canonical/audit/data_center.sqlite`（6.5 MB；核对时 1117 runs、1117 jobs，本文定稿时约 1,136；`journal_mode=delete`、`user_version=0`、全库仅 1 个非主键索引）。

## 0. 结论摘要

1. **调度确实是这个仓库当前最大的能力缺口。** 数据生产链路（connector、coverage、window planner、transform、staging/publication、质量与 receipt）已经完整且有测试；缺的不是"怎么生产一个窗口"，而是"**谁决定现在生产哪些窗口、按什么节奏、生产到什么程度算完成**"。今天这个决定分散在三处：代码里的 registry（品种/recipe/policy）、机器级 env（`DATACENTER_MAINTENANCE_SYMBOLS` 8 个品种）、systemd timer（节奏）。改一个品种或换一个节奏要动 systemd + env + 部署，且没有任何地方能看到"当前一共有哪些生产计划"。
2. **"管理所有注册的生产维护计划"目前没有数据模型支撑。** 生产里实际在跑的行情生产计划只有两条：Dukascopy 1m 原始维护（`OnUnitInactiveSec=15min`）与 Dukascopy 1m→5m 派生（macro-market-lab 每天 06:30 UTC 调 Data Center runner）。registry 里注册的 6 个一阶 recipe + 2 个二阶 recipe 中，15m/30m/1h/4h/1d/1w/1mo **没有任何周期性生产者**——不是"配置得不好"，而是从来没被调度过。
3. **spec 的方向是对的，但它把最难的部分当成了实现细节。** spec 用了很大篇幅规定语义（暂停边界、misfire、依赖、固定输入、幂等、验收 AC01–AC24），这很好；但真正决定成败的三件事只在 spec 里各占一句话：① SQLite 基座（三进程写入、无 WAL、无索引、无 schema 版本）；② 计划所有权与唯一性键（谁拥有哪个 selector）；③ 派发预算与公平性如何落库。本文把这三件事展开（4.4/4.8、G2、G7）。
4. **今天的调度器候选接口已经存在，但要"变深"。** `evaluate_task()`（无副作用预览）、`build_ingest_plan()`/`plan_maintenance()`（纯窗口规划）、`ingest_window_payloads()`（展开成可重试 run）、`TransformExecutor.derive()`（已接受 snapshot 对象）都是干净的内部 seam；缺的是把它们收进一个拥有状态机与事务边界的模块，而不是让 HTTP adapter、CLI runner、systemd timer 各自拼 payload。
5. **有一个正在流血的生产缺陷与调度直接相关**：派生执行时用"重新解析当前 catalog 并与提交时的 snapshot_id 比较"来保证输入一致，一旦比较失败就是 `ValueError` → `retryable=False` → 该 run **永久 failed**（`backend/src/data_center/ingest/process.py:35,55-61`）。而原始层每 15 分钟推进一次。只要派生工作开始排队，这个竞争就会变成系统性失败。spec 的"持久化固定输入"要求正是解药，但需要新增"由持久化 part 引用重建 `CatalogSnapshot`"的能力（4.6）。

## 1. 事实基线

### 1.1 计划定义分散在三处

| 维度 | 今天的权威来源 | 证据 | 后果 |
| --- | --- | --- | --- |
| 品种范围 | 机器级 env `DATACENTER_MAINTENANCE_SYMBOLS=EURUSD,GBPUSD,USDCAD,USDJPY,AUDJPY,GBPJPY,XAUUSD,BTCUSD` | `backend/src/data_center/settings.py:41,82-89`；仅在 CLI 未传 `--symbols` 时生效 `maintenance_runner.py:514-515` | 改品种要改主机 env 并重启 timer 相关单元；WebUI 看不到 |
| 可维护产物 | 代码注册的 recipe（`utc-24x7-1m-to-{5m,15m,30m,1h,4h,1d}-ohlcv@1`、`utc-24x7-1d-to-{1w,1mo}-ohlcv@1`） | `platform_registry.py:104-123` | 与品种的组合是笛卡尔积，但实际只有 5m 有生产者 |
| 节奏 | systemd timer | `deploy/systemd/market-data-center-1m-maintenance.timer:5-7`（`OnBootSec=5min` + `OnUnitInactiveSec=15min`） | "上一轮结束后 15 分钟"，不是固定相位；改节奏要走部署 |
| 治理参数 | 代码 policy（`dukascopy_1m`: tail 2 天、shard 60 分钟、lag 180 分钟、gap cooldown 180 分钟、max_window 31 天） | `platform_registry.py:43-45` | 所有品种/任务共用一套，无法按任务调 |
| 派生范围 | macro-market-lab 桥接脚本把 `--symbols <8> --recipes utc-24x7-1m-to-5m-ohlcv@1 --start now-2d --end now-1d` 作为默认值（可用环境变量覆盖） | `/home/quant/repos/macro-market-lab/scripts/marketlab-maintain-market-bars-data-center.sh:9-11,25-30` | 派生只覆盖 5m 且滞后一天 |

### 1.2 实际在跑的周期性生产计划（主机实测）

| 计划 | 归属 | 触发 | 数据域 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| `market-data-center-1m-maintenance` | Data Center | 启动后 5 分钟 + 上轮结束后 15 分钟，`Persistent=true` | dukascopy 1m BID raw，8 品种 | active，最近一轮 pass（8 目标 / 3 degraded / 0 failed） | `operations/market_data_1m_maintenance/`（190 份 receipt） |
| `marketlab-market-bars-maintenance` | macro-market-lab → DC runner | 每天 06:30 UTC | dukascopy 1m→**仅 5m** | active | `operations/derived_market_bars_maintenance/`（21 份） |
| `market-data-center-monitor` | Data Center | 约 120 秒（配置 60 秒 + `AccuracySec=1min` 合并） | 容量/告警出箱 | active | `operations/monitor/`（2915 份） |
| `market-data-center-smoke` | Data Center | 每 5 分钟（无 Persistent） | API 存活 | active | 仅 journal |
| `market-data-center-provider-acceptance` | Data Center | 每天 01:00 UTC；`ExecStartPost` 跑 `operations retention-audit` | 真实 provider 抽样 | **红**：2026-09-11T03:39 起连续失败 | `acceptance/receipt-*.json` |
| `marketlab-econ-maintenance` / `marketlab-event-calendar-maintenance` | macro-market-lab | 每天 07:10 / 08:10 UTC（仓库模板） | 经济观测 / 事件日历 | **未安装**（主机无此单元） | 仓库 `deploy/systemd/user/` |

两个直接结论：

- 真实 provider 验收变红后，`retention-audit` 作为 `ExecStartPost` 一起停了（最近一份 `operations/capacity_check/` 是 2026-09-11T03:58）。这是"计划的健康度与依赖关系没有被统一管理"的现实后果，调度器接管时必须把保留审计与验收解耦。
- macro-market-lab 的桥接脚本用仓库 checkout 的 `.venv` 与 `PYTHONPATH`（脚本 `:6-7,23`），与 `docs/current-state.md:12` 记录的"生产进程不再引用任何仓库 checkout"相矛盾。接管派生调度是修掉它的自然时机。

### 1.3 底层基座现状（决定实施顺序的硬事实）

| 事实 | 证据 | 对调度的影响 |
| --- | --- | --- |
| ledger 无 WAL、无 `busy_timeout`、无 `synchronous` 设置，全库仅 `quality_findings_dedupe` 一个非主键索引 | 实测 `journal_mode=delete`、`user_version=0`；`runs/ledger.py:18-23` | API、worker、monitor、将来的 scheduler 是**四个写者**共用一个 rollback-journal 库；写事务会互相阻塞 |
| `jobs` 无 `(status, available_at)` 索引，`claim_next_job()` 每次拉全部 queued 行到 Python 过滤 | `runs/ledger.py:317-335` | 到期扫描与领取都是有界性风险；AC14 的"扫描有界"无法在现有 schema 上达成 |
| 任务+多 run+审计不是原子写：`enqueue_ingest_plan()` 逐窗口 `enqueue_job()`，随后 `upsert_maintenance_task()` 与 `record_write_audit()` 又各开一次连接 | `platform.py:128-134`、`maintenance_tasks.py:499-506` | "接受一轮执行"没有事务边界，中途崩溃会留下半个执行 |
| 暂停是字符串前缀匹配：`payload["job_id"].startswith(task_id)` | `runs/ledger.py:323-325` | 相邻命名会误伤；没有全局暂停、没有优先级、暂停后重新提交会被 upsert 重置为 queued（`ledger.py:83`） |
| run 读模型把全部 runs 读进内存再过滤分页 | `run_views.py:148,181,186,303`；实测约 1,100 runs 时读取 + 解析 ≈ 400 ms | 计划历史与"每个计划的 run 列表"不能建在这个读模型上 |
| deployment 逻辑哈希写死了要审计的表清单 `("runs","jobs","quality_findings","dead_letter_state","dead_letter_audit")` | `deployment.py:438` | 新表默认**不进**部署身份哈希；不改这里，scheduler 状态的变化对 deployment 校验不可见 |
| 无 `user_version`、无迁移框架，DDL 由每个进程在构造时执行 | `runs/ledger.py:17-31` | 新表/新列必须补"可重复、可验证"的迁移机制，否则多进程同时 DDL 是隐患 |
| 派生执行对输入的快照校验失败即永久失败 | `ingest/process.py:35,55-61` | 见 0.5；这是调度器必须解决的正确性问题，不是优化项 |

### 1.4 现有 spec/plan 已经覆盖得很好的部分

为避免重复劳动，以下不再展开，直接沿用：领域对象划分（ProductionTask / TaskExecution / Step / Run / TaskProgress / SchedulerState）、五类计划语义（manual / once / interval fixed_rate / interval fixed_delay / calendar daily）与 DST 规则、窗口边界暂停的 5 条规则、coalesce 不等于丢数据范围、依赖与 publication 推进、lease + fencing、`preview/create/change/read/reconcile` 接口雏形、HTTP 契约 §8、AC01–AC18。

## 2. 调度功能需求分解

按"能力域 → 需要什么 → 今天 → 目标"组织。D1–D8 覆盖用户提到的全部诉求（新增不同品种、覆盖时期、运行时间、暂停/继续、新增/删除、管理所有已注册计划）。

### D1 计划注册表与目录同步（"管理所有注册的生产维护计划"）

- **需要**：一个权威的"计划清单"，而不是三处分散定义；每条计划有稳定身份、归属人、目标产物、数据范围、节奏、状态、上次/下次执行；能回答"某个 registry 里已批准的品种/recipe 有没有计划在维护它"。
- **今天**：`maintenance_tasks` 表只有 1 行（单次维护记录），没有长期定义；品种来自 env，recipe 来自代码。
- **目标**：`production_plans`（长期定义）+ 只读的"注册表矩阵"视图，展示 `已批准品种 × 已注册 recipe` 的覆盖情况，并标注：已计划（哪个计划）/ 未计划 / 不可用（未批准、recipe 不适用该 provider）。这是"管理所有已注册计划"的可视化依据。

### D2 计划时间语义

- **需要**：manual / once / interval(fixed_rate, fixed_delay) / calendar(daily, hourly)；首次启动时间与数据起点是两个独立字段；DST 正确处理；错过的触发合并但不丢数据；执行耗时不影响 fixed_rate 锚点。
- **今天**：`schedule: Literal["manual"]` 硬编码（`maintenance_tasks.py:83,176`；`runs/ledger.py:92`），节奏只存在于 systemd。
- **目标**：纯函数的时间模型（注入时钟），`next_after(now) -> datetime | None` + `describe(now)`；预览未来 5 次；全部以 UTC 存储，时区只在 calendar 计划里作为定义的一部分。

### D3 数据范围与窗口（"覆盖时期"）

- **需要**：每个产物有自己的"已发布范围 / 未闭合缺口 / 待补区间"；历史起点一次性补齐；停机后按预算追赶而不是每 tick 建一个 run；tail 复查与 gap 修复共存；容量门禁不被小分片绕过。
- **今天**：窗口在提交时按固定 `[start,end)` 冻结（`platform.py:97-125`），没有 backlog、没有追赶、没有 per-output 边界；`plan_maintenance()` 已经有良好的纯规划 seam（`control_plane.py:482-551`），但输入端必须由调用方给出 `coverage`。
- **目标**：`TaskProgress`（per-output frontier / gaps / catch-up span）+ 持久化 backlog；每次 tick 只展开有预算的窗口；近端 tail 与历史补齐各有配额。

### D4 依赖与产物（raw → derived）

- **需要**：用户勾选 5m/1h/1d 等产物时，系统自动补齐中间依赖（1d → 1w/1mo），按真实依赖拓扑推进，gap 修复后重算受影响下游；发布事实可恢复（进程崩溃也能补上派生）。
- **今天**：`TransformRecipe` 有 `input_recipe_id`（仅 1w/1mo 用到），但 `dependency_graph()` 只是 dataset→edges 的粗读模型（`control_plane.py:222-238`）；派生 runner 按"层"循环并逐层重新解析 catalog（`derived_maintenance_runner.py:212-223`），没有持久化步骤与依赖状态。
- **目标**：`production_steps` + 依赖边 + publication 游标；步骤是唯一可恢复的执行单位。

### D5 生命周期与操作

- **需要**：新建（含批量品种展开为独立计划）、预览、编辑（乐观锁）、暂停、继续、立即执行、重试失败步骤、复制、归档、**删除**；全局暂停/继续。
- **今天**：只有"提交一次维护"与"按前缀屏蔽 queued 作业"；PATCH 只接受 `paused|enabled`（`api/app.py:510-520`）。
- **目标**：见 4.7 的生命周期与 4.7.3 的删除语义（spec 只写了归档，删除是本次新增需求，见 G1）。

### D6 执行与并发

- **需要**：到期只接受一次逻辑执行；批量入队原子；scheduler 可竞争、可接管；暂停后 worker 不再领取该计划的作业；不同计划公平轮转，单个大 backfill 不饿死其他计划。
- **今天**：`flock` 保证单 worker；`claim_next_job` 无 lease/无 fencing、无优先级；多窗口提交非原子。
- **目标**：`production_executions` 上的 lease + fencing token；派发预算与轮转落库；批量子事务。

### D7 可观测与证据

- **需要**：统一的调度视图（是否允许派发、积压、最老到期延迟、退避、占用中的计划与 worker）、每个计划的下次时间/进度/数据时效/具体阻塞原因、可追溯到 run/manifest/finding 的历史、结构化 receipt。
- **今天**：`/operations/*` 已有队列、容量、worker 心跳；但没有"计划"这一层，也没有把 timer 纳入视野。
- **目标**：`GET /operations/scheduler` + 任务详情投影；receipt 沿用 `operational-receipt.v1`。

### D8 治理、接管与回滚

- **需要**：不改 canonical 身份；旧入口（Data Center 1m timer、macro-market-lab 派生桥接）与新计划不能双重生产；切换/回滚有证据；scheduler 进入 immutable release 的身份与健康检查。
- **今天**：`docs/current-state.md:15,41` 记录了所有权切换与 `MARKETLAB_MARKET_BARS_BACKEND=legacy` 回退开关；发布只能走 immutable activation（`docs/release-checklist.md`）。
- **目标**：见第 5 节 S5 与第 6 节证据策略。

## 3. 现有 spec 的缺口（G 编号，建议先修订 spec）

| 编号 | 缺口 | 为什么重要 | 建议 |
| --- | --- | --- | --- |
| G1 | **只有归档，没有删除** | 用户明确要求"可以新增、删除"；spec §3.2/§4 只定义 `archived`，且声明不删除数据 | 区分"删除计划定义"（允许，带守卫）与"删除数据/历史"（禁止）。定义删除前置条件、墓碑与所有权释放，见 4.7.3 |
| G2 | **没有定义计划所有权键** | AC01 要求"重复输出所有权被拒绝"，但没有说"什么算重复"；今天两个任务可以指向同一 selector 而不被阻止 | 定义 `ownership_key = (dataset_id, provider, symbol, timeframe, price_basis)`（经济数据用 `series_id`），在 `enabled/paused` 计划间唯一；归档/删除释放 |
| G3 | **"所有注册的生产维护计划"的边界** | spec 明确把 monitor/smoke/acceptance/backup 排除在接管范围外（§1），这是对的；但如果没有只读清单，统一视图会"看起来管了其实没管" | 新增只读 `governed_job_inventory`：数据生产计划进计划表，其余治理型单元以"所有者 + 触发 + 最近 receipt"登记展示，不接管 |
| G4 | **registry 漂移未定义** | recipe/instrument/policy 是代码常量，随 release 变化；计划版本里若只存 id/version，跨 release 后可能指向被改写的语义 | 计划版本持久化 `config_digest`（`build_ingest_plan` 已产出 7 个 digest）；tick 时比对，漂移则 `health=config_drift`、停止新派发并要求重新确认 |
| G5 | **SQLite 并发基座未纳入 spec** | 三/四个写者 + rollback journal + 无索引；spec §7.2 只说"不要持写锁做慢操作"，不足以支撑 AC14 | 把 WAL、`busy_timeout`、短写事务、`(status, available_at)` 等索引、SQL 侧读模型列为实施前置（4.8） |
| G6 | **无 schema 版本与迁移机制** | spec §9 要求"migration 可重复、保留旧数据"，但代码里没有 `user_version`，DDL 分散在方法体内 | 引入顺序迁移列表 + `user_version` + 打开时校验；迁移只在单一入口执行 |
| G7 | **预算与公平性没有落库** | spec §7.3 只有原则（轮转、配额、上限） | 定义 `budget`/`rotation cursor` 的持久化字段与每 tick 决策顺序（4.5） |
| G8 | **缺少"计划 vs 注册表"的缺口视图** | 用户要"管理所有注册的生产维护计划" | D1 的矩阵视图 + `unplanned_outputs` 读模型 |
| G9 | **身份迁移未定义** | 今天 task_id 是 `{run_kind}-{provider}-{symbol}` 派生名（`maintenance_tasks.py:159`），新计划用 UUID；旧 URL/文档引用会失效 | 计划表保留 `alias`（旧 task_id）做兼容查询；批量导入时写入 |
| G10 | **固定输入只到 snapshot_id，未到 part 列表** | 见 0.5；`process.py:55-61` 的比较失败是永久失败 | 持久化 part 引用并支持重建 `CatalogSnapshot`（`transform.py:derive` 已接受该对象），执行不再依赖"当前 catalog 恰好未变" |
| G11 | **未处理既有计划的健康缺陷** | 真实 provider 验收红 → `retention-audit` 停跑 3 天 | 在 D8/只读清单中显式暴露，并在接管清单里解耦 `ExecStartPost` |
| G12 | **接管清单缺少"仓库 checkout"依赖项** | macro-market-lab 桥接用 repo `.venv`/`PYTHONPATH` | 接管时改为 release venv + `DATACENTER_DEPLOYMENT_MANIFEST`，与 `market-data-center-1m-maintenance.service:8-12` 对齐 |
| G13 | **WebUI 契约缺少删除与阻塞原因枚举** | 前端要按后端能力渲染 | `/capabilities` 增补计划类型、最小周期、可维护产物、以及计划 `health`/`block_reason` 的枚举 |
| G14 | **重试路径未与计划归属联动** | `ledger.retry_run()` 直接复制原 job payload 重新入队（`ledger.py:400-421`），会绕过计划版本、暂停与 selector 约束 | 生产归属的 run 重试统一走模块 `change(...retry...)`；`/runs/{id}/retry` 识别归属后转交 |

## 4. 深化设计

### 4.1 模块边界：一个深模块，三个适配器

```
                        ┌──────────────────────────────────────┐
   HTTP /production/*   │        ProductionService（深模块）    │   scheduler_main
   WebUI ───────────────▶  preview / create / change / read     ◀──────────────  tick 循环
                        │  reconcile(now, budget)               │
   CLI runner ──────────▶                                      │
                        └──────────────────────────────────────┘
                            内部 seam：ScheduleSpec · WindowPlanner · OutputGraph
                                       OwnershipIndex · Budget/Fairness
                                       Store(ledger adapter) · Clock · CapacityGate
```

接口（保持小；实现可以很厚）：

```python
preview(definition, *, now) -> Preview                 # 纯计算 + 只读 coverage，无写入
create(definition, *, actor, idempotency_key) -> Task  # 建计划（含批量展开前的单条语义）
change(task_id, command, *, expected_version, actor, idempotency_key) -> Task
read(query) -> Page[TaskView]                          # 列表/详情/执行历史，SQL 侧分页
reconcile(now, *, budget) -> TickReport                # 调度器唯一写入口
```

`command` 是一个封闭集合：`update | pause | resume | run_now | retry | archive | delete | enable | disable`。

**删除测试**：如果删掉这个模块，复杂度会重新分散到 HTTP 路由（拼 job payload、算下一次时间）、scheduler 进程（自己判断暂停/依赖）、CLI runner（另一套调度真相）和 `/runs/{id}/retry`（绕过计划约束）。所以它挣得存在。**反向约束**：不要在这个模块里加 provider 分支，也不要为只有一种实现的存储搭插件框架（spec §7.1 已明确）。

### 4.2 内部 seam 与依赖注入

| seam | 沿用今天的实现 | 需要新写 |
| --- | --- | --- |
| `ScheduleSpec` | — | 五类计划的时间计算与描述（纯函数） |
| `WindowPlanner` | `control_plane.plan_maintenance` / `plan_tail` / `coverage_for_rows` / `platform.build_ingest_plan` / `platform.ingest_window_payloads` | 动态 `effective_end`（由 `planning_at - lag` 推出）、backlog 展开、预算切分 |
| `OutputGraph` | `TransformRecipe.input_recipe_id` / `input_dataset`、`select_recipes`、`transform.recomputation_plan` | 真实拓扑排序 + 环检测 + 自动补中间依赖 |
| `InputPinner` | `Catalog.resolve()` 产出的 `CatalogSnapshot`、`TransformExecutor.derive(input_snapshot=...)` | 持久化 part 引用 + 由引用重建 snapshot（G10） |
| `OwnershipIndex` | — | 所有权键唯一约束与冲突检查（G2） |
| `Budget/Fairness` | — | 每 tick 配额、最老到期轮转、历史补齐配额（G7） |
| `Store` | `RunLedger` 的 claim/finish/fail/receipt 语义 | 批量原子入队、计划/执行/步骤表、lease + fencing、SQL 侧读模型 |
| `Clock` | — | 注入时钟（今天 `time.time()`/`datetime.now()` 内联在 `ledger.py:311,386,454`） |
| `CapacityGate` | `CapacityPolicy.inspect/require_ingest_capacity/require_backfill_capacity` | 计划级"无人值守补齐跨度"门禁（AC08） |

原则：**依赖注入只注入会变化的东西**（clock、roots、policy、store），不要给 planner 注入"provider 策略对象"之类的伪扩展点。

### 4.3 状态机

**计划**：`desired_state ∈ {enabled, paused, archived, deleted(墓碑)}`；`phase ∈ {initializing, catching_up, maintaining}`；`health ∈ {healthy, lagging, blocked, attention, config_drift}`（只读投影，不能反向改用户意图）。

**执行**：`pending → running → completed(outcome=pass|degraded|failed|skipped)`，以及 `running → pausing → paused → running`；无在途窗口的 `pending` 可直接 `paused`，零工作量直接 `completed/skipped`。等待依赖/容量/退避用 `block_reason` 表达，**不写进 outcome**。

**步骤**：`planned → ready → submitted → published → done`，另有 `blocked(dep|input|capacity) / failed / superseded(被重算取代)`。

不变量（与 spec §10 一致，本文补充第 4 条）：① 一个计划最多一个非终态 execution；② 每个计划时隙与每个步骤提交幂等；③ 暂停先提交则不再产生新派发或 claim；④ **所有权键在同一时刻只能有一个非归档计划**（G2 新增）。

### 4.4 数据模型（同库增量，`production_` 前缀避免与现有表混淆）

```text
production_plans(plan_id PK, alias, name, desired_state, phase, current_version,
                 owner_key,
                 created_by, created_at, updated_by, updated_at, deleted_at)
production_plan_versions(plan_id, version, definition_json, config_digests_json,
                         created_by, created_at, PK(plan_id, version))
production_executions(execution_id PK, plan_id, plan_version, trigger_source,
                      scheduled_for, schedule_revision, idempotency_key,
                      state, outcome, block_reason, planning_at, effective_start, effective_end,
                      coalesced_count, first_scheduled_for, lease_owner, lease_expires_at,
                      fencing_token, started_at, finished_at,
                      UNIQUE(plan_id, schedule_revision, scheduled_for),
                      UNIQUE(plan_id, idempotency_key),
                      UNIQUE(plan_id) WHERE state IN ('pending','running','pausing'))
production_steps(step_id PK, execution_id, ordinal, stage, dataset_id, provider, symbol,
                 timeframe, price_basis, recipe_id, recipe_version, window_start, window_end,
                 input_ref_id, depends_on_json, state, block_reason, publication_run_id,
                 UNIQUE(execution_id, dataset_id, recipe_id, recipe_version, window_start, window_end))
production_step_runs(step_id, submission_generation, run_id, PK(step_id, submission_generation))
production_input_refs(input_ref_id PK, dataset_id, selector_json, digest,
                      parts_json, part_count, created_at)      -- 可重建、可分页
production_progress(plan_id, output_key, frontier_end, published_ranges_json,
                    open_gaps_json, pending_ranges_json, publication_cursor,
                    PK(plan_id, output_key))
scheduler_state(id=1, dispatch_enabled, instance_id, heartbeat, last_tick_at, last_error)
scheduler_leases(plan_id PK, owner, fencing_token, expires_at)
```

索引（按查询形态建，不建"以防万一"的索引）：

```text
production_plans(desired_state, current_version)
production_executions(state, lease_expires_at)          -- 接管扫描
production_executions(plan_id, started_at DESC)         -- 计划历史
production_executions(state, scheduled_for)             -- 到期扫描（配合 plans.desired_state）
production_steps(execution_id, ordinal)
production_steps(state, execution_id)
jobs(status, available_at)                              -- 补现有表（G5）
jobs(owner_plan_id) WHERE owner_plan_id IS NOT NULL     -- 部分索引，claim 时反连接
```

对现有表的最小改动（兼容、可回滚）：

1. `jobs` 增加 `owner_plan_id`、`owner_step_id`、`owner_execution_id`（可空）——把它作为暂停/所有权的**第一类字段**，取代 `job_id` 字符串前缀匹配。
2. `runs` 增加**列**（不是 JSON 内字段）：`plan_id`、`execution_id`、`step_id`、`status`、`created_at`——否则 AC14 的"有界扫描"无法实现（今天 `runs` 只有 `payload`）。
3. `runs` 的列与 `payload` 必须由同一事务写入，terminal receipt 仍不可改写。

### 4.5 tick 算法（`reconcile(now, budget)`）

```text
1. 读 scheduler_state：dispatch_enabled? 否则只做收口，不新派发。
2. 收口阶段（无长事务）：
   a. 对 state ∈ {running, pausing} 的 executions，按 step→run 关联读取终态 receipt，
      更新 step/execution/progress（publication 游标 +1）。
   b. 解除依赖：上游 step published → 下游 step ready（同一事务内批量）。
   c. lease 过期（expires_at < now）→ 允许他者接管；旧 owner 迟到提交因 fencing_token 被拒。
   d. 全部 step 终态 → 写 execution outcome；fixed_delay 在此计算下一次时间。
3. 到期扫描：按 (desired_state='enabled', next_run_at <= now) 索引取前 N（N=budget.scan_limit）。
4. 对每个到期计划（最老到期优先 + 历史补齐配额轮转）：
   a. 事务 A：抢占规划权——upsert scheduler_leases（lease + fencing_token+1），
      创建/复用唯一 execution（唯一约束保证"至多一个非终态"）。
      pause 与 claim 的先后关系由这个事务的提交顺序决定（spec §5.3-5）。
   b. 事务外：读 coverage（parquet）、容量、registry digests、provider 退避状态；
      若权威状态已变（desired_state/global pause/definition_version/lease/token）→ 放弃。
   c. 计算计划：原始窗口 → 依赖图 → 步骤集合 → 预算裁剪。
   d. 事务 B：写 steps + step_runs + runs + jobs + audit + 所有权校验 + 推进 next_run_at
      （fixed_rate/daily 前移；once 清空；零工作量记 skipped）。全部成功或全部回滚。
5. 派发：受"每计划最多一个已派发未终结步骤"约束，从 ready steps 取最老者入队。
6. 观测：写 tick receipt（时长、到期延迟、合并次数、积压、阻塞分类、接管次数）。
```

事务纪律：**事务 A/B 内不做 parquet 读取、不做网络、不等待 worker**（spec §7.2 已要求，这里明确到具体事务）。

### 4.6 幂等与一致性（三层）

| 层 | 键 | 落点 |
| --- | --- | --- |
| 触发去重 | `(plan_id, schedule_revision, scheduled_for)`；手动用 `(plan_id, idempotency_key)` | `production_executions` 唯一约束 |
| 步骤→run 提交去重 | `(step_id, submission_generation)` | `production_step_runs` 主键 |
| 物化去重 | `(dataset, selector, recipe@version, window)` 的已发布 manifest 检查 | 沿用 `coverage` ready 短路与 `_manifest_covers`，但改为按窗口索引而非全量 manifest 扫描 |

**固定输入（G10）**：`production_input_refs.parts_json` 存 `[(relative_path, run_id, schema_version)]`；执行时用它们重建 `CatalogSnapshot`（`PartReference` 已是 dataclass，见 `catalog/snapshot.py:15-33`），`transform.derive` 接收的正是这个对象。这样"提交后新增了 raw part"不再导致永久失败，而是"按既定输入完成 + 把新数据加入待重算集合"。

### 4.7 生命周期细则

#### 4.7.1 暂停 / 继续
沿用 spec §5.3 的 5 条规则，落地点改为：`jobs.owner_plan_id` + claim 时与 `production_plans.desired_state` 的反连接（部分索引），而不是字符串前缀。补齐两条 spec 未写的：
- 暂停中的计划被 PATCH 编辑配置：编辑允许，但**不解除暂停**（防止"改配置顺带恢复生产"）。
- 暂停期间 `run_now`：返回 `409 task_paused`（spec 有），若已有在途执行则返回该执行 id（spec 有）。

#### 4.7.2 归档
`archived` 是软删除：不再派发、释放所有权、保留全部历史与数据；`GET /production/tasks` 默认不返回，需显式 `state=archived` 查询。归档前置：无非终态 execution（spec §3.2）。

#### 4.7.3 删除（G1，本次新增）
分两种，产品上必须区分清楚：

| 动作 | 语义 | 前置条件 | 保留 |
| --- | --- | --- | --- |
| `archive` | 停止使用，可恢复 | 无在途执行 | 计划定义 + 执行历史 + 数据 |
| `delete` | 删除计划**定义**（墓碑） | 必须已 `archived` 或 `paused`；无非终态执行；所有权已释放 | run/receipt/manifest/canonical 数据、`write_audit`（含 actor、时间、definition 摘要）、墓碑行（`deleted_at`） |
| 删除数据/历史 | **不支持** | — | 只能走已有的容量治理与保留策略，不在本功能范围 |

删除必须记审计（`action=production.plan_deleted`，带 definition 的 sha256），且删除后 alias 仍可解析到墓碑，避免旧链接变成 404 而无解释。

#### 4.7.4 编辑与复制
`definition_version` 乐观锁；只有 `schedule / outputs / window_policy / ownership` 形成新版本；`name` 等展示字段即时更新。`provider/symbol` 不可改，只能复制成新计划（spec §5.3）。复制默认 `paused`，必须先释放/转移所有权。

### 4.8 必须一并修改的 substrate（否则功能无法验收）

1. `PRAGMA journal_mode=WAL` + `busy_timeout`（建议 5s，保留 Python 默认值即可但要显式）+ 写事务保持毫秒级；WAL 需要与现有 backup（`operations.py:74-90` 用 `sqlite3.backup()`）和 restore 一起回归验证。
2. 顺序迁移：`user_version` + 迁移函数列表 + 打开时校验"库版本 ≤ 代码版本"，并补"从 v0 库升级"的测试。
3. `deployment._sqlite_logical_hash` 的表清单（`deployment.py:438`）必须加入新表，否则部署身份看不到 scheduler 状态（G5）。
4. SQL 侧读模型：`runs` 增列；`RunStore.list_by_plan(plan_id, cursor, limit)` 走索引；**不改**现有 `/runs` 契约。
5. 批量原子入队：`RunStore.enqueue_batch(payloads) -> list[run_id]`，内部一次连接一次事务；`enqueue_ingest_plan` 改为调用它。旧的 `enqueue_job` 保留（单窗口路径与测试仍用）。
6. 时钟注入：`RunLedger(..., clock=...)` 或方法参数化；`available_at`/`heartbeat`/`created_at` 全部走注入时钟，便于 misfire 与 DST 测试。
7. `DeploymentService.service_names`（`deployment.py:138-141`）是构造参数元组：新的 `market-data-center-scheduler.service` **必须加进去**，否则 activation 不会重启/等待校验它，`/health/ready` 的部署身份比对也不会覆盖 scheduler（spec §9 要求"部署后 WebUI、API、worker、scheduler 身份与 release receipt 一致"）。
8. 调度 receipt 的 action 名必须加进 `operations_views.RECEIPT_ACTIONS`（`operations_views.py:18-21`），否则 `GET /operations/receipts` 永远看不到它。

### 4.9 HTTP / WebUI 要点（相对 spec §8 的补充）

仓库既有约定（新接口必须直接复用，不要另起一套）：

- envelope `{data, meta:{request_id, schema_version:"v1"}, errors}`（`api/app.py:154-163`）；错误码 `401/403/404/409/422/507`（`app.py:349-352`）；容量保护 507 的 `data` 里带 `write_status` 与 `capacity`（`app.py:406-415`）。
- 分页沿用 HMAC 签名、TTL 3600s、绑定筛选条件集的 opaque cursor（`run_views.py:62-101`），`meta.page` 形状不变；越界或换筛选即 422 `cursor_error`。
- 写审计覆盖"被拒绝的提交"（`app.py:186-232`），actor 指纹规则见 `app.py:166-183`；计划操作（含删除）必须同样入审计。
- 新路由必须进 `backend/tests/test_api_surface_contract.py` 的 `MUTATING_ROUTES`（含 `requires_key` 与 `writes_audit` 决策），否则该测试直接失败（`:98-107`）——这是仓库强制的安全门禁，实施时必须先改测试分类表。
- 新增 `deploy/systemd/market-data-center-scheduler.service` 的 `EnvironmentFile` 必须位于 `%h/.config/market-data-center/` 下，否则 `scripts/production_env_check.py:37-44` 失败。
- `/capabilities` 增补：`schedule_types`、`min_interval_minutes`、`outputs`（可维护 recipe 列表）、`plan_health_reasons`、`block_reasons`（枚举），前端按能力渲染，不猜。

WebUI 的具体落点（现状窄且清楚，改动面可控）：

- 没有 router：新视图 = 改 `Tab` 联合类型（`components/shell.tsx:9`）、`items` 数组（`shell.tsx:10-15`）、渲染分支（`main.tsx:34-42`）三处。按 spec §3.2，生产计划视图放在 Maintenance 工作区内部作为同级入口（"生产计划 / 单次维护"），Operations 只保留健康入口并链接过去。
- `schedule` 在 UI 与线上类型里都被钉死为 `"manual"`（`pages/MaintenancePage.tsx:135`、`lib/api.ts:340`），任务抽屉无条件显示 `t("Manual")`（`MaintenancePage.tsx:329`）——这是要替换的**唯一**单点断言。
- 任务列表服务端无分页无筛选（`lib/api.ts:700`），过滤在前端做（`MaintenancePage.tsx:325-326`）；AC14 的规模必须走新的 `GET /production/tasks`（cursor 分页），不要扩展旧接口。
- 进度今天只有状态徽章 + 1500ms 轮询 run detail（`hooks/index.ts:204`）：新视图需要"已完成窗口 / 当前已规划窗口 + 每个产物的完整边界"，并显式禁止合成百分比。
- 时间与时长：所有时间戳必须走 `<TimeDisplay>`（`preferences.tsx:12`），`TimeMode = Asia/Shanghai | UTC | dual`；**目前没有时长格式化器**（`OperationsPage.tsx` 多处各自拼字符串（如时长与能力展示）），"距下次运行 / availability lag / 到期延迟 / backlog 年龄"应共用一个新 helper；日期输入→ISO-UTC 的转换已经在 4 个页面各写一遍，计划表单不应成为第 5 份。
- 认证实际来自 `mdc_session` cookie（`lib/api.ts:629` + auth 面板），`apiKey` prop 形同废弃；写操作失败以 401/403/507 呈现——计划操作按钮的可用性要由后端能力 + 计划状态共同决定，而不是前端猜测。
- 刷新目前以手动为主（`main.tsx:22-24`），页面内只有 header 时钟（`shell.tsx:22`）与运行跟踪器两处轮询；调度心跳/到期延迟视图需要新增独立轮询，且不能复用"任务抽屉 90 次上限"的轮询语义。
- 中文化：新增文本必须进 resources 并保持 key parity（`docs/specs/2026-09-15-webui-deep-chinese-localization.md`）。

产品上必须能回答的四个问题（计划详情的信息架构）：

1. **下次什么时候跑**（含 fixed_delay 的"完成后 N 分钟"这类不可预测语义）；
2. **跑到哪了**（per-output 已发布边界、未闭合缺口、待补跨度）——禁止用"完成百分比"表达仍在增长的 backlog；
3. **为什么没跑**（`block_reason`：容量 / 依赖 / 输入不可用 / provider 退避 / 暂停 / 配置漂移）；
4. **上次结果与关联 run**（run → manifest → finding 的可追溯链）。

操作按钮集合由后端能力 + 计划状态共同决定：`enabled → pause, run_now, edit, copy`；`paused → resume, edit, copy, archive, delete`；`archived → delete`；任何状态下 `running` 都提供"查看当前执行"而不是新建一轮。

## 5. 实施路线（S0–S5）

阶段编号与实施计划一致（S0–S5）；本文的贡献是按"风险最小的可独立合并单元"给出排序理由，并新增**影子模式**（S2）：在真正入队之前，先把调度决策写成可核对的 receipt。

| 阶段 | 交付 | 关键点 | 可独立合并 | 对应 AC |
| --- | --- | --- | --- | --- |
| **S0 基座** | 迁移框架（`user_version`）、WAL/busy_timeout、批量原子入队、时钟注入、`jobs` 归属列、`runs` 列 + 索引、deployment 哈希表清单 | 不改任何外部行为；对现有生产库做升级演练与备份/恢复回归 | 是 | AC05 基础、AC22、AC24 的基础部分 |
| **S1 计划注册表（只读+写入但 scheduler 关闭）** | `production_plans/versions`、所有权键、`preview/create/read/change`、WebUI 计划列表与详情、`/capabilities` 增补 | 用户能创建/查看/暂停/编辑/归档/删除计划，但**不会自动跑**；这是"管理所有计划"的第一步价值 | 是 | AC01、AC07、AC16、AC19、AC20、AC23 |
| **S2 时间与影子调度** | `ScheduleSpec`、`reconcile()` 的决策路径、`/operations/scheduler`、tick receipt | scheduler 运行但 `dispatch_enabled=false`：只记录"此刻会派发哪些计划/窗口/步骤"，不写 jobs。用真实 coverage 核对决策正确性 | 是 | AC02、AC03、AC14 |
| **S3 真实派发与执行** | `production_executions/steps/progress`、lease + fencing、暂停/继续/run_now/retry、backlog、预算与公平 | 先以 1 个 canary 品种 + 单产物接管；`jobs.owner_plan_id` 让暂停真正生效 | 是 | AC04、AC06、AC08、AC13 |
| **S4 依赖闭环** | `OutputGraph`、`production_input_refs`、publication 游标、重算集合 | 先只做 raw；再接 5m；最后接 1d→1w/1mo；每步都用隔离 worker 做真实发布/readback | 是 | AC09–AC12、AC15 的部分、AC18 的跨 provider 部分 |
| **S5 接管与默认启用** | 1m raw timer → 计划；macro-market-lab 派生桥接 → 计划（改为 release venv）；`retention-audit` 与 provider acceptance 解耦；回滚演练 | 切换前阻止旧入口新提交、收口在途窗口、核对 ledger；保留 `MARKETLAB_MARKET_BARS_BACKEND=legacy` 回退 | 否（需要审批窗口） | AC15、AC17、AC18、AC21、AC24 |

顺序理由：S0/S1 不改变生产行为却能先交付"能看见、能管理计划"的用户价值；S2 用影子模式把调度决策的正确性证据拿到手再动数据；S4 的依赖闭环放在真实派发之后，避免一上来就同时调试调度与派生正确性。

## 6. 验证与证据策略

| 层次 | 做法 | 关键断言 |
| --- | --- | --- |
| 时间模型 | 注入时钟 + DST 用例（缺失/重复时刻）、跨月/跨年 daily | 预览与实际一致；执行耗时不影响 fixed_rate 锚点 |
| 并发与故障 | 多进程（不是线程）竞争、lease 过期接管、事务中断、响应丢失、pause×claim 两种顺序 | 只接受一次逻辑执行；旧 owner 迟到提交被拒；暂停后无新 claim |
| 数据集成 | 隔离 canonical/ledger/evidence + 真实 worker 子进程 + fixture provider | raw 与所选 derived 可查询；gap 修复触发下游重算；固定输入在新增 part 后仍可重建 |
| 性能 | 100 个混合计划 + 多年历史的合成库（并按 1,000 计划 / 100k runs 做超出规格的压力探针） | 到期扫描与派发 DB 段 P95 < 5 秒；无长写锁；单计划失败不影响他者 |
| 浏览器 | pinned Playwright，隔离根 | 创建→预览→暂停→重启→继续→删除 全流程；按钮集合随状态变化 |
| 统一门禁 | `bash scripts/ci.sh all`（Python 3.10/3.11/3.12、Node 22） | 与现有 CI 一致；新路由进 `MUTATING_ROUTES` |
| 生产接管 | S5 的切换/回滚演练 | 新旧入口无双写；回滚不领取暂停作业；receipt 绑定 commit/部署身份 |

## 7. 风险登记册

| 风险 | 触发条件 | 影响 | 缓解 |
| --- | --- | --- | --- |
| SQLite 写竞争 | 4 个写者 + rollback journal | `database is locked`、tick 抖动、worker 领取延迟 | S0 的 WAL + 短事务 + 索引 + 压测（AC14 前置） |
| 双重生产 | 旧 timer 未停就启用新计划 | canonical 重复发布风险、receipt 语义混乱 | 所有权键 + 切换前阻止旧入口 + 在途收口核对 |
| 暂停失效 | 依赖 job_id 前缀的实现被沿用 | 暂停期间仍在生产 | `jobs.owner_plan_id` + claim 反连接；专门的多进程测试 |
| 派生永久失败 | 见 0.5 的 snapshot 比较 | 需要人工干预的 dead run | `production_input_refs`（S4 前置，实际上可以提前到 S3） |
| 大面积补齐压垮容量 | 首次为 8 品种 × 7 产物建立计划 | 容量 critical、长时间占用唯一 worker | backlog + 预算 + 公平轮转；默认新建计划为 `paused`，逐个启用 |
| registry 漂移 | 计划跨 release 后 recipe/policy 变化 | 语义静默改变 | 版本内持久化 `config_digests`，tick 比对（G4） |
| 读模型坍塌 | 用 `ledger.list()` 支撑计划历史 | 内存/延迟随历史线性增长 | SQL 侧列 + 索引 + cursor（S0） |
| 接管遗漏 | 只停本仓库 timer | macro-market-lab 仍在生产派生 | 清单化两个项目的入口（本文 §1.2 + spec §9） |

## 8. 决策记录

2026-09-14 已确认四项（均已写入 spec：§1「已确认的设计决策」、3.4、5.1、7.3、9.3）：

| 决策 | 结论 | 落点 |
| --- | --- | --- |
| 治理型 timer 是否进统一视图 | 纳入，但只读；清单是观测投影而非配置来源，须报告"仓库声明 vs 主机已安装"差异 | spec 3.4 |
| 首版并行度 | 每 ledger 一个数据 worker；并行度作为计划与全局的策略字段，默认 1；重新评估的触发条件写入 spec | spec 7.3 |
| 新建周期计划默认节奏 | `fixed_rate` 15 分钟；旧 timer 导入保留 `fixed_delay=15m`；单轮常超周期时可改回 | spec 5.1 |
| `retention-audit` 解耦 | 作为接管前的先行独立小发布，带独立 receipt（并登记到运维 receipt 视图的 action 列表），不得并入接管批次 | spec §9 第 3 条 |

仍待确认：

1. **开工时间与范围**：计划对齐已在同一次修订中完成（plan 使用 S0–S5 并映射 AC19–AC24）；何时开始 S0、以及是否先单独发布 `retention-audit` 解耦，仍由维护者安排。

## 9. 附：本文与现有 spec/plan 的对照

| 主题 | spec | 本文补充 |
| --- | --- | --- |
| 领域模型与状态 | §4 完整 | 4.3 不变量 + 4.4 落库表/索引/约束 |
| 时间语义 | §5.1–5.2 完整 | 4.2 `ScheduleSpec` 作为 seam；测试矩阵 |
| 暂停/继续 | §5.3 五条规则 | 4.7.1 落地点改为所有权列；补编辑不解除暂停 |
| 生命周期 | §3.2 到归档为止 | 4.7.3 删除语义（新增） |
| 依赖与派生 | §6 完整 | 4.6 固定输入可重建的具体做法（G10） |
| 可靠性 | §7 完整 | 4.5 tick 事务边界 + 4.8 substrate 前置（G5/G6） |
| HTTP 契约 | §8 完整 | 4.9 仓库强制门禁（MUTATING_ROUTES、EnvironmentFile） |
| 接管 | §9 完整 | §1.2 实测清单 + G11/G12 |
| 验收 | §11 AC01–AC24 | §6 补齐性能/故障注入的具体断言 |
