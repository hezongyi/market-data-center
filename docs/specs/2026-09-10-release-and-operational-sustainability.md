# Release and Operational Sustainability Specification

日期：2026-09-10
状态：complete；R1-R5、本地与 hosted CI、真实容量/恢复、dependency refresh、immutable release 与 post-release rollback rehearsal 全部通过
前置 spec：`2026-09-10-economic-pit-and-operations`

## 目标

把已经通过 production readiness 与 economic PIT 验收的实现转化为可重复发布、持续验证并能在数据规模增长时安全运行的版本。重点覆盖运行时支持矩阵、依赖可复现性、浏览器验收、容量门禁以及流式和原子备份恢复。

本阶段不增加新的数据 provider。Release/CI module 对维护者提供单一验证入口；operations module 通过小 interface 隐藏容量检查、备份格式、流式 I/O、校验和恢复冲突处理，调用方不直接操作 tar member 或 canonical 文件。

## 当前基线

- `economic-pit-operations-20260910` 已合并到 `main`，merge commit 为 `679dafff539d8e64938cd098f61088e173ae114d`。
- GitHub Actions `Checks` post-merge run `34442430045` 于 2026-09-10 在该 commit 上成功。
- 本地统一 CI 包含 61 个 backend tests、ruff、secret scan、compatibility check、operations acceptance、Web UI build 和 service acceptance。
- Python package 声明支持 3.10+，hosted CI 当前只运行 Python 3.11。
- Web UI 有 browser acceptance 脚本，但 Playwright 没有作为 Web UI 开发依赖进入统一 CI。
- 生产容量验收记录 free ratio 约 11.7%；2026-09-10 当前挂载点约 89% 已使用，需建立明确门禁。
- Backup v1 能验证字节和拒绝覆盖不同内容，但创建过程直接写最终路径，restore 会将单个 member 完整读入内存。

## 范围

### 1. Release closure 与版本治理

- 更新 README、当前 spec 状态和运行手册，使其反映 Binance、yfinance、FRED、economic PIT、worker、告警与恢复能力。
- 建立 release checklist：版本号、commit、统一 CI、hosted post-merge check、兼容矩阵、迁移状态、回滚方式和证据路径。
- R1 完成后为当前已验收基线发布 `v0.1.0` tag/release；tag 必须指向受保护 `main` 上已通过 required check 的 commit。
- Release note 明确 API、dataset schema、manifest、receipt、Python 和 Node 支持版本。
- 不通过修改历史 receipt 或移动 tag 修正发布；修正版本使用新 commit 和新 tag。

### 2. Runtime 与依赖可复现性

- Hosted CI 至少覆盖 Python 3.10、3.11 和 3.12；Node 22 作为 Web UI 构建基线。
- 提交可机器验证的 Python dependency lock/constraints artifact，包含 transitive dependencies；本地 CI 与 hosted CI 使用相同 artifact。
- `pyproject.toml` 继续表达支持范围，lock/constraints 表达已验收解析结果；两者不得相互替代。
- 增加定期 dependency refresh workflow，升级必须通过 connector contract、schema compatibility、provider mock、service acceptance 和 browser acceptance。
- 第三方 warning 必须被消除或进入带依赖、原因、到期时间的 allowlist；不得长期忽略所有 warnings。
- CI workflow 设置最小权限、合理 timeout 和并发取消，避免重复 branch runs 无限制占用资源。

### 3. Web UI 验收

- 将 Playwright 固定为 Web UI dev dependency，并提供 `npm test` 或 `npm run test:e2e` 的稳定入口。
- Browser acceptance 在全新 checkout 和无全局 npm package 的 hosted runner 上可执行。
- 至少覆盖 readiness、dataset list、run filter、失败 run retry、未授权写请求、bars coverage 和移动端布局。
- 如果 Query spec 实现 Economic Explorer，则补充 current/PIT、as-of validation、mixed schema metadata 和分页测试。
- Browser test 使用隔离 ledger/canonical root，不连接真实 provider，不读取本地 `.env.local`。

### 4. 容量门禁与告警

- Settings 增加可配置 warning/critical free ratio；初始默认值分别为 15% 和 10%，部署可使用更严格阈值。
- `retention-audit`、metrics 和 monitor 使用同一容量计算逻辑，避免多个调用方重复实现阈值判断。
- Warning/critical event 使用稳定 event ID；重复 monitor 运行不得产生重复 webhook 副作用。
- 容量低于 warning 时禁止无人值守的大范围 backfill；低于 critical 时拒绝新 ingest，但 read path 和恢复操作继续可用。
- 禁止自动删除 canonical part、manifest、terminal receipt 或 ledger。任何清理策略必须是独立 spec 和显式人工门禁。
- Operations runbook 记录扩容、归档、暂停 ingest、恢复验证和解除告警步骤。

### 5. 原子流式备份与恢复

- Backup 创建写入同目录临时文件，完成 fsync、metadata/hash 验证后原子 rename 到最终目标。
- 中断或失败只留下可识别的 temporary artifact；temporary artifact 不得被 restore 当作有效备份。
- Restore 以固定大小 buffer 流式复制 member，不得把任意大小的数据文件完整载入内存。
- Restore 写入临时文件，校验 size/hash 后使用不覆盖语义发布；目标已存在且字节相同则幂等跳过，不同则 fail closed。
- Backup format v2 明确 format version、源 commit、创建时间、文件列表、size/hash、canonical identity 和 ledger identity。
- Restore 继续兼容 backup v1；停止兼容必须有迁移工具和独立证据。
- 生产备份 destination 必须位于 canonical root 之外，并在 runbook 中要求独立挂载点或等价故障域；同盘副本不能作为唯一恢复副本。

### 6. 可观测性与证据保留

- CI、dependency refresh、browser acceptance、capacity check、backup、verify 和 recovery drill 都生成结构化 receipt。
- Receipt 至少包含 commit、环境、命令/动作、开始与完成时间、版本、结果、失败阶段和安全错误类别。
- Metrics 增加最近成功备份时间、最近恢复演练时间、容量状态和临时备份数量；不得暴露绝对 secret path 或凭据。
- Evidence 保留策略继续至少 90 天；release receipt 与兼容矩阵随版本长期保留。

## 不变量

1. 本地与 hosted CI 使用相同的主入口和依赖解析结果。
2. 声明支持的每个 Python 版本都必须在 hosted CI 中实际运行，而不是只在文档中列出。
3. 告警、备份和 evidence 失败不得把成功 ingest 改写为失败，也不得修改 terminal receipt。
4. 容量保护可以阻止新写入，但不得删除、覆盖或自动压缩 canonical 历史。
5. Backup/restore 始终校验 bytes 和 hash；不同内容的已有目标不得被覆盖。
6. Release tag 只指向受保护 `main` 的已验收 commit，且不可移动。
7. `.env.example` 不包含机器专属绝对路径、内网代理地址或可被误认为生产默认值的配置。

## 实施阶段与门禁

### R1：文档与 release 基线

- 同步 README、完成状态和 post-merge CI 证据。
- 增加 release checklist 和 release receipt schema。
- 清理 portable `.env.example`，保留机器专属配置于 ignored local file。
- 在受保护 `main` 上完成最终 CI，并创建 immutable `v0.1.0` tag/release。

门禁：新开发者只依据 tracked 文档即可完成安装、fixture ingest、API/Web UI 启动和统一 CI；`v0.1.0` receipt 可追踪到 commit 与成功 CI run。

### R2：Runtime 与前端 CI

- 增加 Python 3.10/3.11/3.12 matrix 和 deterministic dependency artifact。
- 将 browser acceptance 加入统一 CI，声明 Playwright dependency。
- 处理 anyio warning 或建立有到期时间的精确 allowlist。

门禁：三版本 backend checks、Web UI build 和 browser acceptance 在 clean hosted runners 全部通过。

### R3：容量保护

- 实现共享 capacity policy、metrics、alerts 和 ingest/backfill gates。
- 在隔离环境验证 warning、critical、恢复和告警幂等。

门禁：阈值上下边界测试通过；warning 阻止无人值守 backfill，critical 阻止新 ingest；readiness 明确区分 read availability 与 write protection。

### R4：Backup v2

- 实现原子创建、流式 restore、v1 compatibility 和跨故障域 destination 验证。
- 执行中断注入、大文件内存测试和真实恢复演练。

门禁：人为终止 backup/restore 不会产生有效的半成品；大文件恢复内存使用与文件大小不线性增长；canonical 和 ledger 字节完全一致。

### R5：版本发布

- 汇总 R1-R4 receipt、兼容矩阵和 consumer 状态。
- 根据实际 interface 变化提升 semantic version；若保持 additive compatibility，预期发布 `v0.2.0`。
- 在受保护 `main` 上完成最终 CI，创建 immutable tag/release。
- 从 release artifact 执行一次安装、启动、smoke 和 rollback rehearsal。

门禁：release receipt 可从 tag 追踪到 commit、CI run、依赖 artifact、backup format、API/schema compatibility 和回滚命令。

## 验收证据

- Python support matrix 的 hosted CI runs；
- Dependency lock/constraints 重建与升级 receipt；
- Clean-runner browser acceptance receipt；
- Capacity warning/critical 与幂等告警测试；
- Backup interruption、streaming memory、v1 restore 和跨故障域恢复 receipt；
- `v0.1.0` release receipt 与 post-release smoke；
- 更新后的 README、runbook 和 compatibility matrix。

## 实施记录

- R1：README、portable `.env.example`、release checklist、release receipt schema、compatibility matrix 和 runbook 已更新。`v0.1.0` annotated tag 指向已通过 required check run `34442430045` 的 protected-main commit `679dafff539d8e64938cd098f61088e173ae114d`；GitHub `Release` run `34461900956` 于 2026-09-10 发布 immutable `v0.1.0` release，并附加 `release-receipt.json`。
- R2：提交 Python 3.10/3.11/3.12 完整传递 dependency locks；本机三个解释器分别运行统一 backend CI，均为 87 tests、ruff、`pip check`、compatibility、secret scan 和 operations acceptance 全部通过。Node 22 使用固定 `playwright==1.63.0`，隔离 browser acceptance 覆盖 readiness、dataset list、run filter、失败 retry、未授权写入、bars coverage 和 390px mobile layout。
- R2：第三方 Starlette/AnyIO warning 使用精确 message/module allowlist，记录依赖、原因与 2026-12-10 到期日；其余 warning 作为错误处理。Hosted `Checks` 使用最小权限、timeout、并发取消和 `verify` 汇总 required check；`Dependency Refresh` 定期重建候选 locks 并执行三 Python 版本、service 与 browser acceptance。
- R2：feature commit `ed8685108e0c24ee35bc85ac56f28cf8a92b5dfd` 的 hosted `Checks` run `34455435757` 于 2026-09-10 成功；`backend-python-3.10`、`backend-python-3.11`、`backend-python-3.12`、`web-node-22-browser` 和汇总 `verify` 五个 job 均为 `success`，并保留三个 backend receipt artifact 与 `web-browser-receipts` artifact。
- R2：PR #4 合并为 protected-main commit `35c7ab71c52372d7cb94e5614f95b7271a4bc210`；post-merge `Checks` run `34461900713` 的三 Python job、Node 22 browser job 与 required `verify` 全部成功。首次正式 `Dependency Refresh` run `34461959295` 成功，生成的 Python 3.10/3.11/3.12 constraints 与 committed locks 逐字节一致，并保留四份 dependency/browser receipt 与两张 viewport screenshot。
- R3：共享 `CapacityPolicy` 已接入 Settings、readiness、metrics、monitor、worker、retention audit、ingest/retry 和 backfill。warning/critical 边界、warning 阻止超过 31 天无人值守 backfill、critical 阻止新 ingest/retry、read path 可用和稳定 capacity event ID 均有测试与 operations receipt。
- R4：Backup v2 仅枚举 Data Center published manifests、其 immutable parts 与 ledger；同目录 `.partial` 完成 fsync、流式 size/hash verify 后以 `renameat2(RENAME_NOREPLACE)` 原子发布。Restore 固定 1 MiB buffer 写 `.restore.partial`，校验后 no-replace 发布；v1 compatibility、竞态冲突、中断、目录 fsync 失败和 12 MiB memory-bound restore 均有测试。
- R4：真实环境 capacity receipt 位于 `/home/quant/market_lake/evidence/data-center/release-sustainability-20260910/operations/capacity_check/`，记录 free ratio 约 11.69%、状态 warning、28 个 published parts。跨故障域 recovery drill 从 `market_lake` NFS 到独立 `appdata` NFS，Backup v2 共恢复并逐字节核对 56 个文件，receipt 位于同一 evidence root 的 `operations/recovery_drill/`。
- R5：package/Web UI semantic version 已提升为 `0.2.0`。最终证据 PR #5 合并为 protected-main commit `54f002580a5c72cdd3da57ef12ffa14ea0f6c8c4`，post-merge `Checks` run `34462588514` 的三 Python job、Node 22 browser job 与 required `verify` 全部成功。Annotated tag `v0.2.0` immutable 指向该 commit；`Release` run `34462830609` 成功发布 GitHub release 与 `release-receipt.json`。
- R5：从全新 detached `v0.2.0` checkout 使用 committed py311 constraints 安装后，统一 CI 的 87 tests、dependency/compatibility/secret checks、operations recovery、Web build、Playwright 1440/390、API/worker restart 与 smoke 全部通过。随后从独立 `v0.1.0` checkout 完成 61 tests、operations recovery、Web build、API/worker restart、smoke 与双 viewport browser rollback rehearsal。结构化总 receipt 位于 `/home/quant/market_lake/evidence/data-center/release-sustainability-20260910/operations/post_release_rehearsal/post-release-rehearsal.json`，SHA-256 为 `4d21d45612188d1412c175a2cf2d437bb530fd6bb850ee0859687d5d6c926ebf`。

## 非目标

- 不在本阶段迁移 SQLite、引入 Redis、Kubernetes、对象存储或多用户 RBAC；
- 不新增 provider 或扩大 economic dataset 范围；
- 不自动删除 canonical 历史来解决容量不足；
- 不把同一挂载点上的备份宣称为完整灾难恢复；
- 不重写 Web UI，只补齐可重复验收和必要的 economic 查询能力。
