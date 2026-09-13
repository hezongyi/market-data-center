# Maintenance Scheduler 设计与验收规范

日期：2026-09-14  
状态：planned  
前置版本：WebUI v0.5 任务中心  
关联文档：`docs/specs/2026-09-13-webui-v0.5-usability-and-access.md`

## 背景

v0.5 提供维护任务注册、手动提交、状态展示和暂停/恢复。hourly/daily 目前只作为后续调度能力的候选配置，不能让界面暗示系统已经具备自动执行能力。本规范用于单独设计、实现和验证真正的计划任务生命周期。

## 目标

1. 到期任务只被可靠地 enqueue 一次。
2. enabled、paused、failed、running 等状态具有清晰且可恢复的生命周期。
3. 服务重启、多个 worker 并行运行时不会重复调度或丢失调度。
4. 每次计划执行都能关联 run、记录结果和错误，并推进下一次执行时间。
5. 调度行为继续遵守容量保护、审计和既有 `/api/v1` envelope。

## 非目标

- 不引入通用工作流平台、复杂 cron 表达式或多租户调度。
- 不改变 canonical 数据、ledger 和现有 worker 的 UTC 语义。
- 不替代 v0.5 的手动任务提交和任务列表功能。

## 设计要求

- 支持 `manual`、`hourly`、`daily` 三种计划；`manual` 永不自动 enqueue。
- 所有计划时间以 UTC 存储和比较，前端通过全局时间模式显示。
- 调度器必须使用持久化 claim/lease 或等价原子条件更新，避免多 worker 重复 enqueue。
- 只有 enabled 任务可被调度；paused 任务不得产生新的计划 run。
- enqueue 成功后原子推进 `next_run_at`；失败时保留错误和可重试状态。
- 记录 `scheduled_at`、`enqueued_at`、关联 run id、完成状态、错误和 actor/source。
- 重启后可从任务 registry 恢复未完成的计划，不依赖进程内内存。
- 调度动作必须写入审计记录，并经过既有容量策略。

## 验收标准

- 到期 hourly/daily 任务在测试时钟下被 enqueue 一次，重复 tick 不产生重复 run。
- 两个并行 scheduler worker 竞争同一任务时，只有一个获得 claim。
- enqueue 成功后 `next_run_at` 按计划推进；失败时保留可诊断错误。
- paused 任务在暂停期间不 enqueue，恢复后按明确规则处理错过的周期。
- 服务重启后，未完成任务和下一次计划仍可恢复。
- 运行历史能显示计划来源、run id、状态、时间和最近错误。
- Python 3.10/3.11/3.12、完整 `bash scripts/ci.sh all` 及隔离验收通过。

## 交付拆分

1. 先补任务 registry 的持久化字段和状态机测试。
2. 实现单 worker scheduler 与幂等 claim。
3. 增加多 worker、重启恢复、暂停恢复和容量失败测试。
4. 最后接入 WebUI 的调度配置和下一次运行展示。
