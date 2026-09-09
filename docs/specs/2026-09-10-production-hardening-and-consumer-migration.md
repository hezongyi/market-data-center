# Production Hardening and Consumer Migration Specification

日期：2026-09-10  
状态：in progress；H1-H5 已实现并完成本机真实验收，当前 commit `9e572e8` 的统一 CI 入口已通过；GitHub Actions hosted run 仍无法取得权威证据，economic/ingest/quality/maintenance consumer 仍按契约保持 `not_migrated`
前置 spec：`2026-09-08-data-center-foundation`、`2026-09-09-production-readiness-design`

## 目标

将当前已通过本地生产就绪验收的数据中心，提升为可持续运行、可审计、可回滚的本地生产数据服务，并完成 `macro-market-lab` 从低风险行情预览到更多只读 consumer 的分阶段迁移。

本阶段的完成标准不是“代码存在”，而是每项契约都有自动化测试、部署配置和真实运行证据。真实 provider、canonical 数据和 consumer 迁移必须使用正式 API、worker、catalog 和 ledger 路径，不得通过临时脚本绕过治理边界。

## 范围

### 1. 生产安全与 CI

- API 默认只绑定受控本机/内网地址；需要跨主机访问时必须配置 `DATACENTER_API_KEY`，写入接口和重试接口均鉴权。
- 密钥继续只从受保护的环境文件或 secret manager 注入；不得进入源码、receipt、日志、Parquet、命令参数或错误响应。
- CI 在托管平台和本地使用同一入口，至少覆盖 backend tests、lint、Web UI typecheck/build、隔离 API/worker service acceptance 和 provider adapter contract tests。
- 分支保护和 required checks 属于部署配置，不改变 API 或数据契约。

### 2. 告警与可观测性

- `/api/v1/metrics` 暴露队列积压、运行状态、重试、dead-letter、超时、成功率和耗时的稳定字段；不得暴露 provider 原始响应或密钥。
- 结构化日志必须包含 `request_id`；与 ingest 相关的日志还必须包含 `run_id`、`job_id`、`attempt`、`provider` 和 `status`（若字段可用）。
- provider acceptance、worker heartbeat 过期、队列持续积压和质量失败产生结构化告警事件。
- 告警发送器必须可配置、可禁用并具备幂等事件 ID；默认本地开发环境只写安全的本地事件日志。外部 webhook、邮件或监控平台接入不得改变数据写入结果。

### 3. 原子发布与临时产物

- ingest 只能先写入 run/attempt staging，再由 supervisor 校验所有 part 的存在、大小、schema 和 hash。
- 一个 run 的全部 part 校验成功后，才通过不可变 manifest 或完成标记对查询层可见；查询不得读到未完成 run 的部分结果。
- canonical 历史 part 永不覆盖。重复 `part_id`、manifest 或完成标记必须幂等返回或明确拒绝，不能静默改写。
- staging 清理只处理已完成、失败或超时且已记录 ledger 状态的目录；运行中的 attempt 不得清理。清理动作需保留数量、字节数、时间和结果的审计记录。

### 4. 回补与数据质量

- 回补必须通过正式 ingest API 入队，每个日期区间产生独立 run、receipt 和新 part；禁止直接写 canonical。
- provider 有单次请求上限时，回补必须分页或按有界区间拆分，支持断点续跑、幂等重试和明确的最大日期范围。
- 回补 receipt 必须记录请求区间、实际覆盖区间、run IDs、行数、hash、质量摘要和失败位置；部分成功不得被标记为整体成功。
- 定期质量检查覆盖 schema、主键重复、OHLC/数值约束、时间连续性、覆盖率和 provider 漂移；质量失败不得发布为 `pass`。

### 5. 经济数据 PIT 契约

- 在迁移任何 economic consumer 前，冻结兼容的 `economic_observations` schema，至少明确 `release_ts`、`asof_ts`、`vintage_start`、`vintage_end`、`availability_policy`、`availability_lag_days`、frequency、units 和 seasonal adjustment 的来源与缺失语义。
- current-state 查询和 PIT 查询必须分别定义排序、可见性和回退规则；未知 release time 不得伪装成已知 release time。
- 未通过字段、时间语义和历史样本 parity 的 economic consumer 必须继续使用原路径，并在输出中标记迁移未完成。

### 6. `macro-market-lab` consumer 迁移

- 所有迁移中的 consumer 通过 `DataCenterClient`/等价只读 HTTP adapter 访问数据，不直接读取 Data Center 的 Parquet、SQLite 或 provider 原始响应。
- 迁移顺序为：provider bars 只读预览和低风险查询；随后是有明确契约的 bars consumer；最后才是 economic、ingest、quality 和 maintenance consumer。
- 每个 consumer 切换前必须完成双读 parity：同一选择器和时间范围比较 row count、min/max 时间、稳定 output hash、质量状态和错误语义。
- 切换使用显式 feature flag 或配置开关；出现 parity 失败、readiness 失败或延迟异常时可回退到旧 consumer，不回写或删除 Data Center canonical 数据。

## 接口与数据契约变更

### API

- 保持 `/api/v1` 前缀和 `data`、`meta`、`errors` envelope 不变。
- `metrics` 可增加字段，但已有字段的类型和含义不得改变；新增字段必须在 contract 文档中登记。
- 如需查询 manifest/coverage/quality audit，新增只读 endpoint；不得让查询 endpoint 触发 provider 网络请求。
- 告警发送和 staging 清理不通过公开 API 直接执行，除非另有鉴权的 maintenance contract。

### Receipt

所有新 run receipt 至少包含：

`run_id`、`job_id`、`dataset_id`、`schema_version`、provider、connector version、request/input hash、output hash、row count、时间范围、`quality_summary`、`attempt_count`、`retry_count`、状态、错误类型、失败阶段、创建/开始/完成时间、发布 manifest 或 part 引用。

失败 receipt 必须安全地记录错误类别和可重试判断；provider 原始 URL、响应正文和密钥不得持久化。

### Manifest

manifest 是 run 级发布边界，至少包含 `run_id`、dataset、schema version、part 列表、每个 part 的字节数和 hash、row count、质量摘要、生成时间和发布状态。manifest 发布后不可变。

## 不变量

1. API、worker、acceptance、backfill 和 maintenance 使用同一配置注入的 canonical root 与 ledger。
2. 查询只能读取已完成且经过 schema/hash 校验的 manifest；不得观察 staging 或半发布 run。
3. Parquet part、manifest、receipt 和 terminal ledger 状态不可被后续执行覆盖。
4. 任何 provider 请求都必须经过 connector、代理/密钥配置和受控超时；provider 原始响应不出现在 API、日志和 receipt。
5. 重试不会修改原始 terminal receipt；新执行通过新的 `run_id`/attempt 关联原 run。
6. 质量失败、覆盖不足、PIT 语义不明或 parity 失败不得自动切换 consumer。
7. retention 默认只审计和清理明确允许的 acceptance/staging 产物；canonical 历史数据和原始业务 receipt 不自动删除。
8. 所有外部副作用（告警、consumer 切换、清理）都必须可审计、幂等并可回滚。

## 非目标

- 不在本阶段引入 Postgres、Redis、Kubernetes 或多用户 RBAC。
- 不实现自动数据修复、自动篡改历史 part 或无人工门禁的全量 consumer 一键切换。
- 不把 `economic_observations.v1` 在未完成 PIT 兼容前宣称为 `macro-market-lab` economic consumer 的等价替代。
- 不要求一次性迁移所有 `macro-market-lab` consumer；每个 consumer 独立完成 parity 和回滚验证。
- 不把外部告警平台、托管 CI 或 provider SLA 作为本地代码可以自行保证的事实；这些需要环境和运营方配置。

## 实施阶段与门禁

### H1：安全、CI 和告警

完成 API secret 注入、网络边界、托管 CI 连接、告警适配器和 metrics 扩展。门禁：secret scan、未授权写入测试、CI 全绿、模拟告警幂等测试。

### H2：原子发布与 staging 生命周期

引入 manifest/完成标记、查询可见性过滤、清理命令和恢复流程。门禁：多 part 中断恢复、重复发布、并发查询和 staging 清理真实测试。

### H3：回补与质量漂移

实现分页/分段回补、断点 receipt、覆盖率和 provider 漂移检查。门禁：短区间真实回补、故障后续跑、重复回补不覆盖、质量失败阻断发布。

### H4：PIT 契约与 consumer 双读

冻结 economic PIT schema，逐个建立 `macro-market-lab` adapter 和 parity receipt。门禁：固定历史窗口的 hash/行数/时间/质量对比和显式回滚演练。

### H5：生产切换

只切换已通过 H1-H4 的 consumer；保留旧路径和回滚开关。门禁：真实 provider acceptance、readiness、延迟/错误预算、告警接收和切换后双读抽样。

## 真实验收标准

本 spec 只有在以下证据全部存在时才可标记完成：

- 托管 CI 与本地 CI 使用同一命令入口，且最近一次运行成功；若无 remote，必须明确标记为未完成而不能以本地结果替代。
- API 在未授权请求下拒绝写入/重试；授权请求、request ID 和 run ID 可在日志与 receipt 中关联，密钥扫描无泄漏。
- 人为中断多 part run 后，查询始终看不到半发布结果；恢复后要么整体发布，要么整体失败并可安全重试。
- staging 清理只删除符合策略的 terminal 目录，并保留审计记录；canonical 历史 part 字节和 hash 未变化。
- 至少一次真实 provider 分页/分段回补跨越多个区间，完成断点续跑和幂等重复验证。
- Binance、yfinance、FRED acceptance 均通过正式 API/worker，receipt 含完整质量摘要、manifest/part 引用和验证命令；失败场景也有受控告警证据。
- `macro-market-lab` 每个已迁移 consumer 都有 parity receipt、真实 HTTP 读取证据、回滚开关和独立测试；未迁移 consumer 清单明确。
- economic consumer 只有在 PIT schema 和历史窗口 parity 通过后才可切换；否则必须保留旧路径。
- systemd service/timer 重启后仍为 enabled/active，readiness、metrics、告警和回补命令在真实环境可执行。

## 回滚与证据保留

- 代码回滚使用版本化 commit；数据回滚不删除 canonical part，而是禁用 manifest/consumer feature flag 并恢复旧读取路径。
- acceptance、backfill、parity、quality、cleanup 和切换报告统一保存在受保护的 evidence 根目录，至少保留 90 天；过期归档必须可校验解压。
- 每次门禁报告记录 commit、环境标识、命令、开始/结束时间、输入范围、结果和失败原因；不得只记录“通过”摘要。
- 任何未满足的门禁都保持 spec 状态为 `in progress`，不得通过修改验收范围来宣称完成。
