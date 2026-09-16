# 开发与阶段验收指南

生效依据：2026-09-15 维护者确认的 EURUSD 优先交付决策。适用于 agent 与维护者共同开发；产品操作说明仍放 `webui-user-guide.md`。

## 1. 先确定当前工作

读取 [当前产品规范](specs/2026-09-15-eurusd-first-product-baseline.md)与[路线图](plans/2026-09-15-eurusd-first-implementation.md)，UI 另读 `webui/AGENTS.md`，预览另读[预览规范](specs/2026-09-15-isolated-preview-environment.md)。旧 spec 的历史状态不代表当前排期，未列入本轮的旧工作默认暂停。

2026-09-16 起同时读取[数据集中心接续规范](specs/2026-09-16-dataset-centered-eurusd-maintenance.md)。P2 已通过用户阶段验收；下一切片 P2.1 仍只做 Dukascopy FX / EURUSD，P3 完善单品种全流程后 P4 才加入 GBPUSD。新模型验收引用 DS01–DS07，旧 P2 receipt 保留但不能证明新行为已通过。

普通切片只需目标、用户动作、验收项和改动范围，不为每个按钮写 spec。新能力或改变数据/接口/权限/预览隔离契约时更新相应 spec；实现方法、文件落点和顺序写 plan；研究证据写 research；已可执行的步骤写 guide/runbook。

用户直接委派的小任务和分析可直接进行，不强制先新建 issue。共享 issue 或多个 agent 同时工作时按[协作约定](agent-collaboration.md)认领；未经授权不向 GitHub/其它人发送消息。保护现有 worktree；广泛变更使用独立分支/worktree。

## 2. 交付循环

1. 说明本切片用户能完成的动作及 E/PV 验收编号；开始相关检查。
2. 尽早提供分支预览，允许在获授权后创建 Draft PR。预览/Draft PR 不以独立 review、全量 CI、tag 或生产发布为前置条件。
3. 发下面的验收卡，维护者可在开发中直接操作。技术评审和产品体验反馈可以并行进行。
4. 处理反馈，更新预览身份；重验受影响部分。相关修正留在当前切片，扩大目标才新拆任务。
5. 达到对应风险的合并门槛后合入 main，更新独立集成环境；未完成入口用 feature flag 管理，不自动推生产。
6. E01–E09 整体旅程接受后准备候选版本，E10 按已授权范围完成版本/生产验证。没有明确反馈不能标记“用户已验收”。等待期间可做不依赖反馈的工作。

每个用户可见的新操作流在合并前应给维护者实际验收机会并记录结果；基础设施切片用技术证据验收，不要求维护者逐项点内部实现。维护者可以明确委托某一阶段验收，记录范围即可，不反复索要相同确认。

```text
阶段：P2 / E03–E04
预览：实际可访问 URL（远程时附访问方式）
身份：branch / commit / dirty 状态 / observed_at
环境：分支预览或集成 / fixture 或 live；数据保留方式
操作：1… 2… 3…
预期：应该看到的结果
已验证：相关检查及对应提交
待完成/限制：没有实现或尚未验证的部分
反馈：待反馈 / 已反馈待修正 / 已接受（人、时间、范围）
```

技术 ready、产品 accepted、merged、deployed 分开记录；不需要新增一套复杂 GitHub 标签。现 `status:review` 可覆盖技术评审和产品反馈，具体状态写验收卡。

## 3. 验证与 review 按风险缩放

| 级别 | 示例 | 本地检查 / 合并前要求 |
| --- | --- | --- |
| R0 低风险 | 普通文档、文案、纯样式，无交互/权限/契约变化 | 自查+相关检查；可免专门独立 review；视觉有变化给预览 |
| R1 普通功能 | 已有契约内的任务页面、局部行为修复 | 相关行为/浏览器检查，一次独立 review（另一 agent 或人类），用户新流程阶段验收 |
| R2 高风险 | 数据身份/聚合规则、并发/恢复、权限、破坏性 schema、发布机制、治理规则 | 独立深入 review，契约/故障恢复证据，完整适用 gate，风险说明和已授权范围核对 |

评审不是创建 PR 的门槛。review 发现需修复或有证据回应；复查受影响部分，不因小修重复整个仓库双轴审查。R2 可采用双轴评审，R0 不默认启动多 agent。普通“用户可见行为改变”不自动升级为 R2；PR 拆小也不能隐藏 R2 风险。

纯文档治理变更属于 R2 的是决策影响和合并前独立复审；本地适用检查为文档一致性、链接和规则冲突检查，不因 R2 标签机械运行与修改无关的 provider/浏览器全套验收。

所有合并仍需 protected-main、当前待合入提交的 hosted `verify` 成功和可合并状态。已有权限/功能授权持续有效；只有新范围或未覆盖的高风险/生产动作需要补充确认。产品整体完成与生产激活是独立检查点，不需要积累一个巨大 PR，也不能默认每个小 PR 都发布。

开发中执行与变化有关的 Ruff/pytest/build/browser 检查，不要求每个 commit 本地重跑 `ci.sh all`。R2 跨模块和版本候选执行完整 `bash scripts/ci.sh all`；正式版本保留 Python 3.10/3.11/3.12、Node 22 和服务/浏览器验证。当前 hosted 仍执行完整矩阵；P0 后续优化重复触发/安装，路径分流尚未实施，不以文档变更假装 CI 已提速。

代码或行为变更后检查证据必须对应当前版本；UI 体验验收受影响时重给预览，不复用旧行为的批准。网络故障/源延迟和 fixture 结果分别报告；默认 CI 不联网访问真实 provider。

### Playwright、Chromium 与 GitHub 排错

Playwright 以 `webui/package-lock.json` 为准，使用 `npm --prefix webui ci`，不要依赖全局包。Hosted CI 用 `npx --prefix webui playwright install --with-deps chromium` 安装浏览器。本 Ubuntu 主机可复用 Chromium 缓存，但应查找实际 executable，不能假设带版本号的目录：

```bash
export PLAYWRIGHT_BROWSER_EXECUTABLE="$(find "$HOME/.cache/ms-playwright" -type f -path '*/chrome-linux/chrome' | sort -V | tail -1)"
export LD_LIBRARY_PATH="$HOME/.local/share/playwright-deps-jammy/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
test -x "$PLAYWRIGHT_BROWSER_EXECUTABLE"
npm --prefix webui test
```

缓存浏览器无法启动时，先用 `ldd "$PLAYWRIGHT_BROWSER_EXECUTABLE" | grep 'not found'` 查缺失动态库，再决定是否下载。Git push 使用 SSH，可用 `ssh -T git@github.com` 和 `git remote get-url origin` 核对。GitHub API/PR 使用持久化 `gh` 登录；不要打印 token：

```bash
export PATH="$HOME/.local/bin:$PATH"
gh auth status
gh api user --jq .login
```

使用 `gh pr create` 创建 PR，使用 `gh pr view` 或 `gh pr checks` 查看状态。合并前确认当前 head 为 clean、最新 `verify` 成功且 workflow `head_sha` 与待合入 commit 一致，不能复用同分支旧提交的成功记录。OAuth token、含凭据的代理 URL 和 API key 只放用户配置或 ignored 文件；机器本地路径不能成为运行依赖，文档可记录不含秘密且可替换的本地参考或证据路径。

## 4. 预览操作与交接

P0 已提供 `scripts/dev-preview.sh` 管理独立本机预览。先按 Python 3.11 约束安装后端，并显式安装 Web 开发依赖；`--include=dev` 即使调用 shell 带有 `NODE_ENV=production` 也不会漏装 Vite、TypeScript 或 Playwright：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -c backend/constraints/py311.txt -e './backend[dev]'
npm --prefix webui ci --include=dev
DATACENTER_PYTHON=.venv/bin/python bash scripts/dev-preview.sh start --id <preview-id>
bash scripts/dev-preview.sh status --id <preview-id>
bash scripts/dev-preview.sh stop --id <preview-id>
```

`start` 返回 UI、API docs、checkout/commit/dirty、fixture 模式、scheduler 有效派发状态、数据根和日志。默认只绑定 loopback；远程操作使用 `ssh -L <ui-port>:127.0.0.1:<ui-port> -L <api-port>:127.0.0.1:<api-port> <host>`。`stop` 保留 `.preview/<id>/data` 和日志；代码身份变化后须用 `start --update` 明确接受新身份。同一 id 不会静默换端口或代码。

默认 `.preview/<id>/` 在 worktree 内且被 Git 忽略：提交/推送只保存管理脚本，不保存 auth、数据或日志；删除整个 worktree 也会删除这些运行数据。需要让运行数据独立于 worktree 生命周期时，启动时传一个外置的受控非生产目录，例如 `/home/quant/repos/.preview`，并在删除 worktree 前停止预览和备份该目录。

本开发主机当前保留的集成预览采用外置目录，供后续阶段继续验收：

```text
preview id: p0-eurusd
preview base: /home/quant/repos/.preview
data/log/auth root: /home/quant/repos/.preview/p0-eurusd
UI: http://127.0.0.1:25345
API docs: http://127.0.0.1:25344/docs
```

后续 agent 必须先读取状态，且每个命令都带同一个 `--base`；省略后会在当前 worktree 的 `.preview/` 新建或操作另一套环境。不得删除或重置外置目录中的 auth、ledger、数据和日志：

```bash
bash scripts/dev-preview.sh status --id p0-eurusd --base /home/quant/repos/.preview
bash scripts/dev-preview.sh stop --id p0-eurusd --base /home/quant/repos/.preview
DATACENTER_PYTHON=.venv/bin/python bash scripts/dev-preview.sh start \
  --id p0-eurusd --base /home/quant/repos/.preview --update
```

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

运行进程仍依赖启动它的代码 worktree 和 Python 环境。删除或替换该 worktree 前先用上述外置 base 停止预览；在新 worktree 安装锁定依赖后，再以相同 id/base 和 `--update` 重启，原持久数据会继续使用。地址以 `status` 的实际输出为准；若端口或运行主机改变，应同步更新本节与根 AGENTS 路由提示。

预览交付必须运行 API/worker/scheduler/Vite，而非只有静态页；模拟内容显著标识。stop 保留数据；浏览器测试不销毁用户预览；更换版本要说明。登录凭据通过适当本地交付方式提供，不写入公共验收卡。

默认预览只允许 fixture connector、关闭告警外发，并为 API、worker、scheduler、auth、canonical、ledger、evidence、backup 和日志提供独立根。页面顶部显示预览身份、模拟数据和 scheduler 心跳。P0 不完成的 P1 路由/shadcn 页面现已交付；P2 手动任务闭环和 PV08 已实现并通过维护者阶段验收。P3 自动两轮/恢复和 P4 第二任务旅程仍未开始，这些限制必须继续显示在阶段验收卡中。

## 5. 文档与发布

spec 描述要求，plan 记录排期，guide 记录已可执行操作，research 保留来源。状态至少区分 approved/implemented/accepted；`排期：paused` 与实现状态独立。对旧 spec 局部取代时在文首链接新规范并列出范围，不改历史 receipt。

当前事实带 observed_at；源码、运行部署、用户接受分别标示。生产身份需要读回，未读回就保留历史快照说明。发布时读 release checklist/runbook；开发预览不套生产激活程序。
