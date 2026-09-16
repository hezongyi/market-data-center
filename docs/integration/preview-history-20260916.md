# 预览历史记录（2026-09-15 至 2026-09-16）

本文件保留原开发指南中的执行事实与地址，仅作历史证据；当前进度见[实施路线图](../plans/2026-09-15-eurusd-first-implementation.md)，实际操作见[开发指南](../development-guide.md)。操作前读回 status，不从历史记录推断当前环境。

P1 任务主线阶段验收保留另一套独立 fixture 预览。以下记录观测于
2026-09-15，功能提交 `5c22c7c` 经 PR #127 合入 main（merge
`2bd5d7f`）；后续收口版本以 `status` 的实际 commit、dirty 和 API identity
读回为准：

```text
preview id: p1-eurusd
preview base: /home/quant/repos/.preview
UI: http://127.0.0.1:24239
API docs: http://127.0.0.1:24238/docs
mode: fixture；四进程；scheduler 有效派发状态可见
feedback: 维护者已验收任务列表、健康筛选、创建、详情刷新/返回及兼容控制台
PV06 recheck: 维护者于 2026-09-15 确认 commit/dirty/clean 与有效派发状态通过
```

验证包括正确工作目录下的 Ruff、API 契约 10 项、后端完整 gate（489
passed、5 skipped）、Web build、隔离预览验收、1440×1000 与 390×844
浏览器流程及 service smoke；PR #127 当前 head 的 hosted `verify` 通过。
任务只保存为暂停状态，fixture 不访问真实 provider；手动数据闭环、真实
Dukascopy、自动恢复和第二品种仍分别属于 P2–P4。检查或更新此预览必须保留
同一外置 base：

```bash
bash scripts/dev-preview.sh status --id p1-eurusd --base /home/quant/repos/.preview
DATACENTER_PYTHON=.venv/bin/python bash scripts/dev-preview.sh start \
  --id p1-eurusd --base /home/quant/repos/.preview --update
```

P2 开发预览使用独立 id `p2-eurusd`，不覆盖 P0/P1 数据。fixture 模式把
Dukascopy/EURUSD 业务身份路由到确定性的本地分钟 adapter，receipt 明确记录
`isolated-preview-fixture-v1`；它不访问 provider。P2 live 验收另用新 id，模式
一经创建不可切换，且命令必须显式给出不超过 24 小时的 UTC 窗口和预算：

```bash
DATACENTER_PYTHON=.venv/bin/python bash scripts/dev-preview.sh start \
  --id p2-eurusd --base /home/quant/repos/.preview

set -a; source "$HOME/.config/market-data-center/dukascopy-preview.env"; set +a
DATACENTER_PYTHON=.venv/bin/python bash scripts/dev-preview.sh start \
  --id p2-eurusd-live-day3 --base /home/quant/repos/.preview --mode live \
  --live-start 2026-09-14T00:00:00Z --live-end 2026-09-15T00:00:00Z \
  --live-request-budget 100 --live-byte-budget-mib 100 \
  --live-runtime-budget-seconds 3600 --inherit-proxy
```

live connector 在每次实际 provider HTTP 请求前核对 provider、EURUSD、窗口、累计请求数、持久化运行时
预算和 canonical 字节数；超限立即拒绝。磁盘检查发生在请求前，因此最多可能超出
单个已限制请求的落盘量，下一次拉取会停止。API 同时只接受窗口范围内的 fixed/manual 任务。默认
fixture 与 live 都关闭告警外发、使用独立 auth/ledger/canonical/evidence/backup；
不得省略 `--base` 操作上述保留预览。`--inherit-proxy` 仅在 live 显式选择时
传入标准代理环境变量，代理值不写 preview metadata 或状态输出。开发机若需持久保存代理，使用
repository 外的权限受限环境文件或 shell 私有环境；仓库忽略的 `.env.local` 不会被 preview
脚本自动加载。SOCKS 代理使用 `ALL_PROXY=socks5h://host:port`，同时用 `NO_PROXY` 排除
`127.0.0.1,localhost`；锁定依赖包含 requests 所需的 PySocks 支持。EURUSD 1m 使用官方
hourly BI5 tick 文件并按 BID 聚合，每个小时文件分别扣减一次 live 请求预算。
瞬时网络错误使用 connector 内固定次数和冷却时间重试；每次尝试仍分别扣减预算，耗尽预算时立即停止。

2026-09-16 的 P2 PV08 实证使用 clean commit `b1bc42a`：UI
`http://127.0.0.1:21933`、API docs `http://127.0.0.1:21932/docs`，execution
`132b61ae-76c8-4880-83ff-97a094bbcc92` 覆盖上述完整 UTC 交易日并以
`completed/degraded` 收口。官方 BI5 发布 22 个 raw 小时（1,320 行）和可查询的 264 条 5m；
每条派生数据均有 `input_snapshot_id`。22:00–23:00 文件真实缺少 22:19、22:29 两分钟，
因此该小时标为 provider gap 且没有补造；实际网络尝试为 35/100。该结果用于核对完整窗口、
固定输入、缺口诚实呈现和预算，而不是“全日无缺口”的承诺。登录凭据按预览交接单提供。

P2.1 数据集中心预览使用独立 id `p21-eurusd-dataset` 与同一外置 base，
不覆盖 P0/P1/P2 数据。它保留数据集、维护请求、production execution、raw/5m
parts 与 ownership 审计；页面可创建固定的 Dukascopy FX / BID 1m 数据集、加入
EURUSD、暂停/恢复并执行固定区间手工补数。操作时仍必须显式传 base：

```bash
bash scripts/dev-preview.sh status --id p21-eurusd-dataset --base /home/quant/repos/.preview
DATACENTER_PYTHON=.venv/bin/python bash scripts/dev-preview.sh start \
  --id p21-eurusd-dataset --base /home/quant/repos/.preview --update
```

2026-09-16 技术验收时 UI 为 `http://127.0.0.1:23639/datasets`、API docs 为
`http://127.0.0.1:23638/docs`，fixture 模式四进程健康；实际地址与代码身份仍以
上述 `status` 读回为准。凭据仅通过本地验收交接提供，不写入仓库。真实 Dukascopy
连接仍因外部代理返回 HTTP 503 而受限，不能把本 fixture 结果报告为 live 成功。
