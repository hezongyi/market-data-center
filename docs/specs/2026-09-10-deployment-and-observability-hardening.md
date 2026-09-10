# Deployment and Observability Hardening Specification

日期：2026-09-10
状态：proposed
前置 spec：`2026-09-10-production-hardening-and-consumer-migration`、`2026-09-10-release-and-operational-sustainability`、`2026-09-10-query-contract-and-scalability`

## 目标

将已经具备版本发布、容量保护、备份恢复和结构化 evidence 的 Data Center，从“可以发布”提升为“部署身份明确、运行代码不可漂移、监控成本有界、指标语义可信且可以原子回滚”的生产服务。

本阶段建立两个深 module：

1. Deployment module 在一个小 interface 后隐藏 release staging、依赖安装、artifact 校验、原子激活、systemd 重启、健康确认和回滚。调用方只选择已验证 release，不直接拼接仓库路径、虚拟环境或 Web UI 目录。
2. Operational snapshot module 在一个小 interface 后隐藏 receipt 索引、容量状态、临时产物计数、运行聚合和最近成功操作查询。API、monitor 和 acceptance 读取同一 snapshot，不各自递归扫描 canonical/evidence 文件树或重新解释 ledger。

本阶段不增加 provider、dataset 或查询能力。Dukascopy D3/D4 只有在本 spec 的部署与容量前置门禁通过后才能进入生产迁移。

## 当前基线与已知问题

- `v0.2.0` 已在 protected `main` 上完成发布，但当前 systemd unit 的 `WorkingDirectory`、`PYTHONPATH`、虚拟环境和 Web UI 路径仍直接指向可变开发 checkout。
- 生产 API/worker 进程可能在 checkout 后续发生修改时继续运行旧的已加载代码；下一次重启则会加载未提交或跨阶段混合的工作区内容。运行中的 commit/version 与磁盘内容之间没有稳定、可查询的 deployment identity。
- 2026-09-10 检查时，开发 checkout 的 HEAD 落后 `origin/main` 14 个提交，同时存在 39 个 tracked 修改和 12 个 untracked 项；该状态不应成为任何生产重启的输入。
- monitor timer 使用 `OnUnitActiveSec=60s`，但实际单次 monitor 运行约 9–12 分钟。运行超过周期后 timer 会在完成时立即再次触发，形成近似连续运行。
- `run_metrics()` 为统计临时备份和最近成功操作，会在请求或 monitor 热路径中递归扫描 `canonical_root.parent` 与 evidence JSON。生产 canonical 位于大容量 NFS，扫描成本随无关文件和 evidence 历史增长。
- 2026-09-10 容量检查显示 `market_lake` 约 89% 已使用、约 11% free，处于默认 15% warning 门禁内。普通读取可用，但超过 31 天的无人值守 backfill 应保持禁止。
- 当前 ledger 累计指标包含生产 ingest、历史验收、故意失败场景和遗留 dead-letter。检查时共 92 个 runs，其中 62 pass、24 failed、6 dead-letter，直接计算得到的 67.4% success rate 不能代表生产数据管道 SLI。
- Browser acceptance 已设计为隔离运行，但本机统一入口在缺少 Playwright browser binary 时只在运行阶段失败，缺少快速、明确的环境 preflight。

## 范围

### 1. Immutable release layout

- 生产运行代码必须来自版本化、只读约定的 release directory 或等价 artifact install，不得从开发 checkout、dirty worktree 或任意 branch 工作目录启动。
- Release identity 至少包含 semantic version、source commit、tag（如有）、artifact hash、Python lock hash、Web UI asset hash、构建时间和部署时间。
- 每个 release 使用独立 Python environment 和对应 Web UI build；不同 release 不共享可被后续 `pip install` 或 `npm build` 原地修改的 runtime 文件。
- Deployment root 通过受控本机配置注入，不把机器专属绝对路径加入 portable `.env.example`。生产可使用独立于 repository 的 `releases/<release-id>` 和 `current` 指针布局。
- `current` 只能指向已经完成 artifact、dependency、configuration 和 smoke 校验的 release。激活使用原子指针切换；失败不得留下半激活状态。
- Release directory 不包含 `.git`、开发缓存、测试 receipt、`.env.local`、canonical 数据或 provider credential。

### 2. Deployment module interface

Deployment module 对维护者提供以下概念 interface；具体 CLI 名称可以在实现阶段确定：

- `stage(source_ref) -> staged_release`：从受保护 commit/tag 构建并校验不可变 release。
- `activate(release_id) -> activation_receipt`：原子切换 current、重启 API/worker/monitor、验证身份和 readiness。
- `current() -> deployment_identity`：返回进程实际运行的 release，而不是 checkout 当前 HEAD。
- `rollback(release_id) -> activation_receipt`：切回已验证的上一 release，并执行同样的健康门禁。

Interface 必须隐藏以下 implementation 细节：release 路径、virtualenv 创建、constraints 选择、Web UI 构建、systemd daemon-reload/restart 顺序、健康轮询、失败恢复和 receipt 写入。systemd unit、运维脚本和 acceptance 不得分别实现这些规则。

### 3. Runtime identity 与漂移检测

- API、worker 和 monitor 在启动时读取同一份 immutable `deployment.json` 或等价 manifest，并在结构化启动日志中记录 deployment ID、version 和 commit。
- `/api/v1/health/ready` 的 additive metadata 至少包含 `software_version`、`source_commit` 和 `deployment_id`；不得暴露绝对 release path、用户名或 credential。
- `/api/v1/metrics` 暴露相同的 deployment identity，使监控数据可以关联到真实运行版本。
- API 与 Web UI build identity 必须一致；Web UI 可以通过只读 endpoint 或构建常量显示当前版本，但不得自行从 git 或文件路径推断。
- 启动时如果 release manifest 缺失、hash 不匹配、版本不一致或配置引用了开发 checkout，进程必须 fail closed，并产生安全的 deployment failure receipt。
- 运行时不持续读取 git 状态。Git 只用于 staging 阶段证明 source ref；激活后 deployment manifest 是运行身份的 source of truth。

### 4. Operational snapshot module

- API 和 monitor 使用统一的 `snapshot(now) -> OperationalSnapshot` interface。Snapshot 至少包含 queue/worker 状态、按 scope 聚合的 run 指标、capacity、临时 backup/restore artifact 数量、最近成功 backup、最近 recovery drill 和 snapshot 生成耗时。
- Receipt 写入 seam 同步维护一个原子 latest/index 视图。初始 implementation 可以使用 SQLite、单独的 append-only index 加原子 latest 文件，或其他本地可恢复结构；调用方不得依赖具体格式。
- Index 只保存可重建 metadata，不替代原始 receipt。原始 receipt 仍是审计 source of truth；index 损坏时可通过显式 maintenance 命令重建。
- `/metrics`、`health/ready` 和每分钟 monitor 热路径禁止对 canonical root、canonical root 的父目录或完整 evidence root 执行无界 `rglob`/全树遍历。
- 临时 artifact 必须只在已配置的 backup destination、restore staging 和 Data Center 自有 evidence 目录中统计；不得扫描共享挂载点上的无关项目。
- Index 不可用时，snapshot 返回明确的 `unknown`/`stale` 状态并告警，不得在请求热路径自动退化为全量 NFS 扫描。
- API 与 monitor 必须消费同一 snapshot 字段和定义，避免容量、最近备份时间或 dead-letter 口径漂移。

### 5. Monitor 调度与告警交付

- Monitor evaluation 与外部 delivery 是两个内部 seam：evaluation 读取 snapshot 并产生幂等事件；delivery 通过 webhook adapter 发送。Webhook 失败不得延长 snapshot 生成、阻塞 ingest 或修改 terminal run。
- Timer 必须按上一次执行完成时间安排下一次运行，或使用等价的非重叠机制。前一次仍在运行时不得启动第二个 monitor，也不得因错过周期而形成无间隔追赶循环。
- Monitor 配置合理的 `RuntimeMaxSec` 或应用级 deadline。超时形成独立、幂等的 `monitor_runtime_exceeded` event 和 receipt。
- Capacity warning 等持续条件使用状态转换或明确的 reminder bucket；相同状态下不得每分钟产生新的 webhook 副作用。
- Delivery 对每个 event 使用稳定 idempotency key，并具有有界批次、有界单次 timeout 和有界总运行时间。大量历史未发送事件不得让一次 monitor 无限线性增长。
- Monitor receipt 记录 snapshot age、evaluation duration、delivery duration、候选事件数、实际发送数、失败数和当前 deployment identity。

### 6. 生产指标语义

- 新 run 在 enqueue 时必须携带明确的 `run_scope`，初始枚举为 `production`、`acceptance`、`migration`、`maintenance`。该字段是 immutable run metadata，不根据 `job_id`、provider 名称或失败类型猜测。
- 生产 API 提交的普通 ingest 默认 `production`；provider acceptance、browser acceptance、故障注入和 contract tests 必须使用隔离 ledger/canonical root，并明确标记 `acceptance`。
- `/metrics` 同时提供 lifetime counts 和有界时间窗口（至少 1h、24h、7d）的 production SLI。默认 `success_rate` 只计算 `run_scope=production` 的 terminal runs。
- Acceptance、migration 和 maintenance 指标分别输出，不得污染生产 success rate、duration 或 dead-letter 告警。
- 旧 ledger 中没有 scope 的记录统一标记为 `legacy_unclassified`。不得通过字符串规则静默重写历史 terminal record；如需人工确认，使用独立 annotation/audit record。
- Dead-letter 需要 `active`、`acknowledged` 或 `resolved_by_run_id` 等独立运维状态。确认或关联修复不得删除原 run、修改原错误或降低历史计数。
- Metrics 必须提供 error category/provider 的有界聚合，避免维护者为定位失败而读取全部 runs 或暴露原始异常正文。

### 7. Capacity 与后续迁移门禁

- 本 spec 保留现有 warning/critical threshold 和“不自动删除 canonical 历史”的不变量，不通过降低阈值伪造容量恢复。
- 当 production storage 仍处于 warning 时，Dukascopy D4、超过 31 天的大范围 backfill 或历史迁移不得以无人值守方式启动。
- 扩容、迁移到新挂载点或显式归档完成后，必须生成 capacity receipt，证明 free ratio、挂载身份、read/write 可用性、backup destination 和恢复路径仍符合 contract。
- 如果容量处理需要删除、压缩、合并或归档 canonical part/manifest，必须另建 retention/archive spec；本 spec 不授权这些动作。

### 8. Developer 与 CI preflight

- 统一 CI 在执行 browser acceptance 前检查 Node 版本、Playwright package 和 browser executable，并在缺失时给出确定的安装或本机缓存发现命令。
- Hosted CI 继续显式安装受锁定的 Chromium；本地 CI 不静默下载大体积依赖，也不假定全局 Playwright。
- Deployment acceptance 在 clean release artifact 上运行，不在 dirty checkout 上以测试通过替代真实部署验证。
- Ruff、dependency lock、三 Python 版本、Web build、browser acceptance、service restart 和 deployment rollback 仍通过统一门禁汇总。

## Interface 与契约变更

### Deployment manifest

Deployment manifest 至少包含：

`deployment_id`、`software_version`、`source_commit`、tag、artifact SHA-256、Python version、constraints SHA-256、Web UI asset SHA-256、创建时间、激活时间、前一 deployment ID 和 release format version。

Manifest 在 staging 完成后不可变。激活和回滚分别写 operation receipt，不修改原 manifest。

### Health 与 metrics

- `/api/v1/health/ready` 和 `/api/v1/metrics` 只增加 additive 字段，保留现有 `/api/v1` envelope。
- Readiness 继续区分 read availability、write protection 和 capacity；增加 deployment identity mismatch/stale operational snapshot 的明确状态。
- Metrics 的原字段若语义会因 `run_scope` 修正而改变，应保留 lifetime legacy 字段并新增明确命名的 production/windowed 字段，避免静默改变 consumer 解释。

### Receipt index

- Receipt index 是派生数据，可删除并重建；receipt 本身仍不可变。
- Index entry 至少包含 action、result、completed_at、deployment ID、receipt relative reference 和安全聚合字段。
- Index 更新必须原子、幂等；receipt 已成功写入但 index 更新失败时，原操作结果不被改写，单独记录 index repair need。

## 不变量

1. 生产进程不从开发 checkout、dirty worktree 或可变 shared virtualenv 启动。
2. 运行身份来自已校验的 immutable deployment manifest，不从当前 git HEAD、目录名或进程启动时间推断。
3. Release 激活和回滚只切换代码与静态资产，不修改 canonical part、manifest、receipt 或 terminal ledger 状态。
4. Metrics、readiness 和 monitor 热路径不递归扫描共享 NFS 或完整 evidence 历史。
5. Operational index 失败不得阻塞查询或改写 ingest 结果；只能使观测字段明确降级为 stale/unknown 并产生告警。
6. Acceptance、migration 和 maintenance runs 不得计入 production success rate；未知历史记录不得被猜测分类。
7. Monitor 和 webhook delivery 有界、非重叠、可审计，且任何失败不影响数据读写结果。
8. Capacity warning/critical 继续保护写入；本 spec 不允许自动删除、覆盖或合并 canonical 历史。
9. API、worker、monitor 和 Web UI 在一次 deployment 中报告相同 version、commit 和 deployment ID。
10. 所有 identity、metrics、logs 和 receipts 不暴露 API key、proxy credential、完整敏感 selector 或机器专属 secret path。

## 实施阶段与门禁

### O1：Deployment identity 与 clean baseline

- 在 `origin/main` 的 clean worktree 中实现 deployment manifest schema、source validation 和 runtime identity loader。
- 更新 API、worker、monitor 启动日志以及 health/metrics additive metadata。
- 明确当前 production deployment 与开发 checkout 的分离目标和配置入口。

门禁：dirty checkout 不能被 stage；缺失或篡改 manifest 时进程 fail closed；API、worker、monitor 和 Web UI identity 一致。

### O2：Immutable activation 与 rollback

- 实现 versioned release layout、独立 environment、原子 current 切换、systemd 激活顺序和失败恢复。
- 将 systemd runtime path 从 repository checkout 迁移到受控 release root。
- 从 `v0.2.0` 或后续受保护 main commit 完成一次 forward deploy、一次 rollback 和再次 forward deploy。

门禁：修改开发 checkout 后重启生产服务，运行 identity 和行为保持不变；任一步激活失败都会恢复上一 release；canonical/ledger hash 不变。

### O3：有界 operational snapshot

- 实现 receipt latest/index、显式 rebuild 命令和限定目录的 temporary artifact registry/scan。
- API metrics 与 monitor 改为读取同一 `OperationalSnapshot`。
- 增加测试，若 metrics/monitor 调用 canonical parent 或 evidence root 的无界 `rglob` 则直接失败。

门禁：在至少 10,000 个 receipt 和生产规模目录 fixture 下，warm `/metrics` P95 小于 500ms、cold 小于 3s；单次 monitor evaluation 小于 5s；性能不随共享挂载点无关文件数线性增长。

### O4：Monitor 与指标语义

- 改为非重叠、完成后计时的 monitor 调度，并增加 runtime deadline。
- 分离 evaluation 与 delivery adapter，限制 delivery batch、timeout 和总预算。
- 引入 `run_scope`、production windowed SLI、legacy classification 和 dead-letter acknowledgment。

门禁：连续至少 60 分钟运行无重叠、无立即追赶循环；故意失败 acceptance 不改变 production success rate；同一持续 capacity 状态不重复产生外部副作用。

### O5：生产迁移与关闭

- 在版本化 release 上运行统一 CI、部署验收、服务重启、监控 soak、capacity check 和 rollback rehearsal。
- 对历史 active dead-letter 逐项保留 acknowledgment/resolution evidence，不删除原记录。
- 更新 operations runbook、release checklist、systemd 安装说明和 Dukascopy 前置状态。

门禁：生产进程 cwd/runtime 不位于 repository；health/metrics identity 与 release receipt 一致；monitor 运行时长持续达标；最新 backup/recovery 信息通过 index 正确返回；protected-main hosted `verify` 成功。

## 验收证据

- Clean worktree、source commit、artifact/lock/Web UI hash 和 deployment manifest receipt；
- Dirty checkout 被拒绝 staging，以及修改 checkout 不影响已部署服务的测试；
- Forward deploy、失败注入自动恢复、rollback 和再次 forward deploy receipt；
- API、worker、monitor、Web UI deployment identity 一致性证据；
- 10,000+ receipt、共享挂载点无关文件和 cold/warm metrics benchmark；
- 证明 metrics/monitor 热路径未调用无界 filesystem traversal 的自动化测试；
- Monitor 连续运行、非重叠、deadline、幂等 capacity event 和 webhook failure tests；
- Production/acceptance/migration/maintenance scope 隔离及 legacy/dead-letter audit；
- 当前 capacity warning、扩容或迁移后的 capacity receipt，以及 Dukascopy D4 是否允许启动的明确结论；
- 本地统一 CI、protected-main hosted `verify`、部署后 smoke 和 rollback rehearsal。

## 回滚

- 代码回滚通过 deployment module 原子切换到上一已验证 release，并重启 API、worker 和 monitor；不得通过 git checkout 修改运行目录。
- Operational index schema 或实现失败时，可以停用新 index、从 immutable receipts 重建，或临时返回 stale/unknown；不得恢复请求热路径中的全量 NFS 扫描。
- `run_scope` 和新 metrics 字段保持 additive。旧 consumer 继续读取 legacy lifetime 字段，直至完成显式迁移。
- 回滚不删除新 release、deployment receipt、index repair receipt、告警事件或历史 dead-letter。

## 非目标

- 不在本阶段新增 Dukascopy 或其他 provider 功能；Dukascopy 继续由其独立 spec 管理。
- 不迁移 SQLite ledger 到 Postgres，不引入 Redis、Kubernetes、Prometheus server 或集中日志平台。
- 不设计 canonical retention、compaction、自动归档或删除策略。
- 不改变 provider bars、economic observations、manifest 或 pagination 数据契约。
- 不以重写 Web UI、引入新的前端框架或建设通用部署平台为目标。
- 不保证第三方 webhook、NFS 或 systemd 本身永远可用；本 spec 只保证失败有界、可见且不破坏数据结果。
