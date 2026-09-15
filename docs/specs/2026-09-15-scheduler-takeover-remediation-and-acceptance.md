# 调度器接管修复与验收补充规范

日期：2026-09-15
状态：in_progress；历史总目标尚未全部验收
排期：paused（2026-09-15）。本规范 TA 条目的保留、替代与延期由[EURUSD 产品规范 §5](2026-09-15-eurusd-first-product-baseline.md#5-旧工作暂停与规范接续)逐项定义；原 ≥4h legacy 对照、crypto 验收和扩面不再是当前开发门槛。下文保留旧约定与历史证据，不授权继续原排期。
关联交付：生产任务与统一调度 S0–S5（issue #109），v0.6.1（PR #119）

## 1. 定位与范围

本文补充[生产任务与统一调度主规范](2026-09-14-maintenance-scheduler.md)的缺口处理、接管与发布验收要求，不替代其功能、状态、数据和 API 契约。AC01–AC24 保持有效；本文以 TA01–TA10 标识此次失败后的补充验收。代码根因与修复步骤进入 issue/[实施计划](../plans/2026-09-14-production-task-scheduler.md)，操作命令进入 [runbook](../operations-runbook.md)，最新生产事实进入 [current-state](../current-state.md)。

目标是恢复可解释、可续跑的窄 canary，补齐发布和新旧入口对照证据，再决定是否扩面。本次不增加 provider、品种、价格语义、并行 worker 或高周期历史回补；不改变 canonical identity，不移动标签，不改写历史 terminal run/receipt，不通过放宽质量门禁制造通过。

本文的起草不代表已执行暂停、恢复、发布或扩面。生产操作按既有授权范围执行；尚未批准的范围、替代验收方法、生产发布与扩面仍遵守[协作门禁](../agent-collaboration.md)和[发布清单](../release-checklist.md)。

## 2. 已核对的失败基线

以下是 **2026-09-15 01:35 UTC 的历史快照**，不代表阅读本文时的实时状态。源码基线为 protected-main `aca73a045275908b4fd710df777f563fef673b90`，tag `v0.6.1`，deployment `aca73a045275-c6470772`，ledger schema 5。执行前须重新盘点，禁止按快照重复切换。

| 事实 | 证据与边界 |
| --- | --- |
| #119 已合并，目标提交 Checks 成功 | [PR #119](https://github.com/hezongyi/market-data-center/pull/119)、[Checks 34916986735](https://github.com/hezongyi/market-data-center/actions/runs/34916986735)；CI 通过不等于真实 provider 验收通过 |
| v0.6.1 已激活 | evidence root 下 `operations/deployment_activate/2026-09-15T012654.491229+0000-148a0c02052a4403b4b44da872c1d558.json`：pass，canonical/ledger 哈希未变；三服务 active |
| 标签存在，但 GitHub Release 未完成 | [Release 34917000586](https://github.com/hezongyi/market-data-center/actions/runs/34917000586)：生成回执时 `ci_run_id` 为空，整数转换失败，发布步骤 skipped；当时 Release API 返回 404 |
| 窄 canary 已启动 | 两个旧 raw/derived timer disabled/inactive；9 条计划均为 `fixed_delay 900`，仅 `legacy-dukascopy-EURUSD-1m` enabled，其余 8 条 paused；进程及账本派发开关均开启 |
| Canary 首轮 failed | execution `9b30a301-deaa-4d14-9e8e-6cc78dab24d7`，raw run `a497384b-ddc2-4b79-b16c-6b99ca40d379`；01:28:27 入队，3 次尝试后于 01:30:04 进入 dead letter |
| 失败响应只有覆盖不足 finding | EURUSD `[2026-09-14T22:14Z,22:28Z)`，预期 14 根、返回 13 根，缺 `22:19Z`；`coverage_not_ready`，coverage 内 `quality_status=pass`、readiness degraded；不能据单次响应认定永久 provider 缺口 |
| 影子观察不足约定时长 | `operations/scheduler_tick/`：2026-09-14 23:09:24 至 09-15 01:28:15 共 311 个 shadow tick，dispatched=0，约 2h19m；09-15 01:28:24 起已真实派发。原 [v0.6.0 发布约定](../releases/v0.6.0.md)要求 ≥4h 对照 |
| EURUSD 无派生配置 | 计划 `bar_timeframes=[]`；其余 8 条暂停计划包含 5m 及其他高周期输出，不能用 EURUSD raw 成功证明派生成功，也不能直接把恢复 8 条等同于仅接管 5m |

该 run 的结构化 `quality_summary`、execution/step 关联、计划版本与切换审计须保留为回归输入依据；复现使用隔离 fixture，不复制生产 canonical 全库、不让测试访问真实 provider。原失败终态在修复后仍保留。

## 3. 缺口、失败与续跑语义

### 3.1 分类与发布边界

复用主规范 §4、§6、§7.3，按结构化错误和 coverage 判定；不得只匹配错误字符串，也不得把所有 `QualityError` 或“空结果”一律降级。

| 条件 | 预期处理 | 不允许的处理 |
| --- | --- | --- |
| Session/calendar 确认整窗关闭 | planner 跳过；执行侧兜底为可解释的关闭窗口结果；不重试，不产生 gap 债 | 访问首行崩溃，或报 provider 故障 |
| 按 availability lag 尚不可用、bucket 尚未闭合 | 保留待处理范围，等待有效边界；不提前派发 | 制造失败 run 或发布不完整 bucket |
| Provider 成功响应，开市且已可用窗口无数据，或仅包含 `coverage_not_ready` | 记录本次观测缺口及覆盖证据；轮次按主规范收口为 completed/degraded，精确 gap 留在 TaskProgress | 仅因该缺口反复 worker retry 并进入 dead letter；把缺口标为完整 |
| 结构性质量错误，或覆盖不足与结构性错误混合 | 明确失败并保留全部 findings；结构性错误不按缺口策略自动重试 | 发现一个 coverage finding 就掩盖其余错误 |
| 瞬态网络、超时、限流 | 沿用有界网络重试/provider backoff；重试耗尽保留失败和关联 | 当作“provider 成功返回但缺数据”，或另起并行副本 |
| 配置无效、固定输入缺失/hash 不符 | 按主规范失败关闭，给出修复入口；不能改用当前输入替代 | 无限重试、静默重新绑定输入 |

1. 一次缺数只证明该次响应不完整；“永久缺口”必须有额外 provider 核验证据。缺口到期仍可复查，不能被永久跳过。
2. 部分响应不得作为完整原窗口发布。若复用旧维护逻辑拆出完整子窗口，每个子窗口必须独立通过原质量、session 与 coverage 门禁并有 manifest；未就绪范围仍是缺口。
3. 原始 run、step、execution、计划详情的原因必须一致且可追溯。Run 沿用现有契约；实现应明确现有字段如何承载分类，不为本补充默默新增公开状态枚举。若必须改变契约，另列兼容性变更。
4. 历史 dead letter 不改写成成功；后续修复通过新 run 与追加式处理记录关联。验收“无新增 dead letter”按候选版本、任务和观察窗口计算，不能用删除或重置历史计数达成。

### 3.2 缺口冷却、推进与 fixed_delay

- 缺口、受阻输出及下次允许复查时刻必须持久化；冷却以有效 provider policy 为准。Dukascopy 当前基线为 180 分钟，测试可注入时钟，不应等待真实 3 小时。
- 单窗口缺口不触发整个 provider 的故障退避，不阻塞其它品种、后续 tail 或同品种其它完整派生 bucket。网络故障退避仍独立有效。
- 冷却内不得因每次 `fixed_delay` 到期而重新提交同一未修复缺口；冷却后按预算复查。缺口仍在则续记债务，修复则重新校验 readiness、推进受影响派生与 lineage。
- 轮次不可永久卡在已确认缺口上，也不可清空 gap/backlog 后假称追平；零工作量或 cooldown-only 轮次应能有界收口并解释原因。
- 保持 `fixed_delay 900`：首次没有 `next_run_at` 时，通过一次受审计的 `run_now` 建立首轮，不改为“启用即跑”。已有活动 execution 时不得重复创建。
- 首轮终态后 `next_run_at=finished_at+900s`；实际领取仍受暂停、配置漂移、容量和退避门禁约束。续轮必须由 scheduler 自动触发，不能用第二次手动触发代替。

## 4. 接管恢复与观察方案

### 4.1 先保留现场，再恢复生产

发现 canary 未通过时，停止扩面，通过受审计的任务暂停或全局暂停阻止新派发；按窗口边界允许在途尝试及 publication 收口。记录动作前后计划版本、两级开关、queued/running/retry 数和所有权，不以停 scheduler 进程代替 worker 暂停语义。

恢复旧入口前，必须验证其实际 runner、release 身份、schema/暂停作业兼容性及所生产的 selectors。基线旧派生 service 已 failed 且曾引用陈旧 checkout，因此“重新 enable timer”不能单独作为可用回退证明。回退顺序为暂停新生产、收口并核对队列/所有权、恢复已验证的旧入口；前向恢复采用相反的所有权交接。两者均不得双写或用旧 ledger 覆盖新历史。

### 4.2 补足影子对照

默认补证路径是：在经过验证、单一写入者的 legacy 路径恢复后，让候选版本 scheduler 以 shadow 模式观察 **新的连续 ≥4 小时窗口**。观察范围内必须有可评估计划和实际 legacy 周期，调度器真实派发为零；空计划心跳不计作有效对照。此路径的生产恢复须在执行前核对既有授权和回退可用性。

对照按同一 provider/symbol/price basis、半开窗口、session、availability lag、coverage 与 policy 比较，至少包含：

- 计划版本、候选 commit、legacy runner 身份、两级开关和 UTC 起止时间；固定延迟的影子首轮以只读模拟时钟/规划器评估，不用生产 `run_now` 制造影子 execution。
- 每个预期周期的候选窗口/跳过原因、实际 legacy run/window、缺口及冷却决策；原始和派生范围分别列出。
- 每项差异及解释，未解释差异数必须为零；时钟锚点差异不得掩盖漏窗、重复写入或范围扩大。
- 心跳缺失、重启及配置变更。影响规划语义的候选版本或配置变更后重新起算；不能拼接不相容窗口凑时长。

原 2h19m 保留为部分证据，后续生产时长不能补算。历史回放可以辅助诊断，但必须证明每个时点的输入、配置和 coverage 可重建，不能用当前 catalog 倒推当时状态。若旧路径不能安全恢复而需用回放等替代 ≥4h 实时影子观察，应先形成具体等价性方案并取得维护者确认；默认门槛不因资料不足自动降低。

整体 tick 耗时与 DB 派发耗时分开记录：主规范 AC14 的 P95 <5s 约束是 DB 派发部分。报告全 tick P95/max、due lag 和有效预算；超预算、持续增长的 backlog 或不可解释的饥饿必须解决，不能把空闲 tick 的低耗时作为负载证据。

### 4.3 分阶段 canary 与范围控制

每次启用前冻结范围清单：task/version、provider/symbol、price basis、raw/derived outputs、recipe/version、history_start、有效 policy、节奏、旧入口对应关系及授权来源。

1. **FX raw 阶段**：保持 EURUSD raw-only。验收一轮受审计的手动首轮和至少一轮自动续轮；至少一轮实际发布新 canonical part 并经 HTTP 查询读回，不能以 skipped/degraded 或无工作量轮次代替成功发布。
2. **派生及 crypto 阶段**：在 FX raw 通过后，对单独批准的小范围进行验证。优先从现有暂停计划中选择一个原已生产 5m 的 crypto 计划，覆盖 raw→5m、固定输入及 lineage；若 crypto 的可用性不满足条件，则分别使用已批准的 FX 派生计划与 crypto 计划，不能略过任一验收。启用前核对其 provider/recipe 可用性。
3. **其余范围扩面**：前两阶段与 TA01–TA09 满足后，提交风险、未解决 gap、证据索引和具体计划/输出清单，取得扩面 go/no-go；逐批恢复并观察至少一轮自动周期。全局继续不得解除单独暂停。

EURUSD `bar_timeframes=[]` 不是派生测试对象，不因验收方便给它增加输出。导入定义中的 15m/30m/1h/4h/1d/1mo 也不等于已经批准启用；主规范首批范围与主机实测不一致时逐项记录，超出 raw/5m 的输出须有明确范围确认。若要将某暂停计划暂限为 raw/5m，使用带 expected_version 的定义更新，保留原版本及恢复方案，不能直接改账本。阶段 2 的选择和启用不能隐含在 EURUSD 原有授权里。

## 5. 发布证据与修复版本

1. v0.6.1 的标签、GitHub Release、stage 和 activation 分别记录状态。现有 activation pass 不能代替缺失的 Release receipt；后补发布须保留真实时间顺序，不追写成“先发布后激活”。
2. 核对目标 tag 的 annotated object、commit 及从 protected main 的可达性。Release 必须引用该 commit 上成功的 `verify`，且有非空、有效的 CI run ID/URL；不得借用旧分支头或其它版本的成功结果。
3. 对 v0.6.1 可在条件满足后重跑现有发布流程；它只能发布相同 immutable tag 的原有代码。空、缺失或不匹配的 CI 证据必须在发布前明确失败，不能跳过检查。已有 Release/asset 不覆盖，重复执行保持幂等。
4. 任何运行时或发布工具的代码修复通过新 PR/commit 交付。需要生产激活的行为修复使用新语义版本和新标签，不移动 v0.6.1，也不在其 immutable release 目录打补丁。本文不预定新版本号。
5. 新候选遵守完整发布/激活/回退门禁；API、worker、scheduler、metrics、WebUI 与 monitor receipt 的 deployment/version/source commit 一致。canonical/ledger 的“未变”只用于暂停并收口后的部署操作边界；真实生产期间允许预期的追加，不要求生产前后整个 ledger 相同。

## 6. 补充验收矩阵

所有条目初始为待验证；已有代码或历史 CI 不能自动填为通过。隔离验证运行仓库统一门禁 `bash scripts/ci.sh all`，覆盖 Python 3.10/3.11/3.12 与 Node 22 的要求不变；真实 provider 观察独立于 CI。

| 编号 / 原 AC | 可判定的通过条件 | 最低证据 |
| --- | --- | --- |
| TA01 / AC12、13 | 隔离 fixture 复现 14 根缺 1 根；仅缺口时轮次 degraded、债务持久化且无由该缺口造成的新 dead letter；结构性或混合 findings 仍失败；闭市、未闭合、网络错误分类正确 | 新旧行为对比、结构化 findings、run/step/execution 结果和完整门禁 |
| TA02 / AC10、12、14 | 缺口冷却内无重复提交，重启不丢债；到期有界复查；其它 tail、完整 bucket 和第二品种推进；补齐后受影响派生/lineage 更新且无重复发布 | 注入时钟的隔离 worker 集成验证、publication/进度关联、预算记录 |
| TA03 / AC02、06、07 | 无首轮的 fixed_delay 由一次 run_now 启动；终态+900s 后自动续轮；暂停/全局暂停及重启无重复领取，恢复优先续未完成工作 | 时钟测试；生产手动首轮及自动次轮的 trigger_source、finished_at、next_run_at、审计 |
| TA04 / AC14、17、18 | §4.2 的连续 ≥4h 有效影子对照完成，零真实派发、零未解释窗口差异；DB 派发预算与负载可判读 | 候选/legacy 身份、带时间的输入与窗口对照、完整 tick/legacy run 索引及统计 |
| TA05 / AC09、15、16 | EURUSD 至少一次真实 raw 发布并读回正确 BID/1m；自动续轮完成；无未解释失败或新增 dead letter，允许有可解释且持久化的 gap 债 | task→execution→step→run→manifest→HTTP readback 链；WebUI 结果与后端一致 |
| TA06 / AC09–12、18 | 经批准的 crypto 与 1m→5m 路径均完成真实发布/读回；派生使用固定输入，gap 只阻塞相应 bucket，自动后续周期可推进 | 范围批准、有效定义/recipe、raw/derived manifest、snapshot/lineage、读回及周期证据 |
| TA07 / AC17、24 | 暂停新生产后无新领取、在途收口；回退及前向恢复无双写、不消费暂停 queued 作业、历史和数据不丢失；旧入口身份可用 | 隔离故障/恢复演练；生产切换回退及前向 receipt、队列/所有权核对，引用仍适用的既有演练时注明基线 |
| TA08 / AC18、24 | v0.6.1 发布缺口完成补证；新修复版本如有，具备自身 commit-scoped verify、annotated tag、Release receipt、stage/activation 及一致身份 | GitHub workflow/Release 资产、永久保留的发布回执、部署和组件身份；失败/恢复时间线 |
| TA09 / AC16、18 | 原失败历史保持不变；TA01–TA08 有可追溯证据，TA10 在扩面前明确标为待验证；current-state/计划/runbook/发布清单一致且区分历史与现状 | 扩面前证据索引与文档 PR；issue 的真实状态、剩余工作及归属；TA10 完成后追加最终索引 |
| TA10 / AC17、18 | TA01–TA09 通过后按明确批准范围逐批扩面，各批至少观察一轮自动周期，无双写/未解释失败/范围扩大 | go/no-go、每批计划版本和产物清单、自动周期及最终 takeover 核对 |

证据索引必须逐项记录 TA 编号、状态、UTC 起止、源码/部署身份、配置版本、预期/实际结果和 receipt/run/manifest 引用；新观察记录按现有 operational receipt 规范保留。缺失证据写“待验证/未找到”，不能写“通过/不存在”。测试使用隔离 canonical、ledger、evidence 和 backup roots；生产只读取证不联系 provider，真实生产验证是独立且受授权的动作。

## 7. 完成与交付规则

- 修复实现完成不等于生产接管完成；`implemented` 需代码、评审与门禁证据，`accepted` 需 TA01–TA10 及主规范仍适用的 AC 全部有证据。
- #109 的关闭不是验收证据。后续实施时应明确恢复跟踪或建立关联的接管收尾 issue，按协作约定认领、更新和释放；代码交付与生产验收可分开记录，不能用关闭代码 issue 隐藏未完成接管。
- 文档收口至少覆盖 current-state 的生产身份/计划归属，实施计划的真实阶段，runbook 的 fixed_delay 首轮、两级开关、失败暂停与可用回退，发布清单的失败及补证链。不能把本规范的目标状态写成已完成事实。
- 任一阶段发生不可解释失败、重复发布、所有权冲突、身份不一致或范围扩大，停止进入下一阶段，保留证据并回到对应修复/验证步骤。新代码或影响规划的配置版本使相关旧验收失效，必须重验受影响条目。

## 8. 修订记录

2026-09-15：依据只读核对结果起草。维护者已认可补充 spec 的方向；本稿将缺口语义、影子补证、raw/派生/crypto 分阶段验收、发布回执与扩面条件整理为可评审条款，尚不声明任何新增生产动作或验收完成。
