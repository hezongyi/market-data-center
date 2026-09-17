# 开发与阶段验收指南

生效依据：2026-09-15 维护者确认的 EURUSD 优先交付决策。适用于 agent 与维护者共同开发；产品操作说明仍放 `webui-user-guide.md`。

2026-09-16 修订：维护者在本次会话明确“认可，继续”，批准默认单代理开发、普通改动自查、高风险改动合并前一次聚焦独立评审。本文 §3 是当前唯一风险与评审矩阵，接续 2026-09-15 的普通功能强制独立 review 和治理变更一律 R2 规则；历史研究提案与验收记录保留，不作为恢复旧门槛的依据。

同日补充授权：维护者明确“验收还是开发的 agent 自己做就可以了，agent 验收通过就继续，不用再询问我”。预览、阶段及集成产品验收统一由负责开发的 agent 执行，及时提供预览地址，按 §2 记录证据后继续已授权流程；用户反馈为可选，不再等待用户验收确认。历史用户验收记录保留。

## 1. 先确定当前工作

读取 [当前产品规范](specs/2026-09-15-eurusd-first-product-baseline.md)与[路线图](plans/2026-09-15-eurusd-first-implementation.md)，UI 另读 `webui/AGENTS.md`，预览另读[预览规范](specs/2026-09-15-isolated-preview-environment.md)。旧 spec 的历史状态不代表当前排期，未列入本轮的旧工作默认暂停。

同时读取[数据集中心接续规范](specs/2026-09-16-dataset-centered-eurusd-maintenance.md)中的 DS 验收编号；旧 P2 receipt 保留但不能证明新行为已通过。当前阶段、已完成项与下一步只在[实施路线图](plans/2026-09-15-eurusd-first-implementation.md)维护，指南与 AGENTS 不重复维护进度。

普通切片只需目标、用户动作、验收项和改动范围，不为每个按钮写 spec。新能力或改变数据/接口/权限/预览隔离契约时更新相应 spec；实现方法、文件落点和顺序写 plan；研究证据写 research；已可执行的步骤写 guide/runbook。

编写或更新 spec/plan 时遵循[规划指南](planning-guide.md)：默认一份 spec 与一份包含切片章节的 plan，近期写细、远期保留目标和依赖。规划 agent 定义需求与交接边界，实施 agent 负责实现、相关测试、预览和自行验收；不因此自动启动子代理或增加审批节点。

用户直接委派的小任务和分析可直接进行，不强制先新建 issue。共享 issue 或多个 agent 同时工作时按[协作约定](agent-collaboration.md)认领；未经授权不向 GitHub/其它人发送消息。保护现有 worktree；广泛变更使用独立分支/worktree。

默认由一个主代理完成分析、实现、相关验证和 diff 自查。只有用户明确要求或已明确授权本任务并行开发时才启动开发子代理；说明独立任务边界，避免争用文件和预览。协作协议的存在不构成启动子代理的授权。

## 2. 交付循环

1. 说明本切片用户能完成的动作及 E/PV 验收编号；开始相关检查。
2. 尽早提供分支预览，允许在获授权后创建 Draft PR。预览/Draft PR 不以独立 review、全量 CI、tag 或生产发布为前置条件。
3. 预览可用后立即发送下面的访问与验收卡，不等全部验收完成才提供地址。开发 agent 自行执行适用的用户操作流、浏览器/API 与结果读回检查，记录证据和结论；用户可随时体验和反馈。
4. 修复验收发现的问题及收到的反馈，更新预览身份；重验受影响部分。相关修正留在当前切片，扩大目标才新拆任务。验收通过后继续已授权流程，不因没有用户反馈而等待，也不询问是否认可或继续。
5. 达到对应风险的合并门槛后合入 main，更新独立集成环境；未完成入口用 feature flag 管理，不自动推生产。
6. 开发 agent 在集成预览完成 E01–E09 整体旅程验收后，按已有授权准备候选版本，E10 按已授权范围完成版本/生产验证。记录“agent 验收通过”，不能标记“用户已验收”；技术 ready、产品 accepted、merged、deployed 分别记录。

开发 agent 同时负责实现和产品验收，无需另开验收子代理。每个用户可见的新操作流在合并前按当前 spec 的 E/PV/DS 编号验证实际操作、呈现与数据结果；基础设施切片用适用技术证据验收。记录执行者、时间、代码身份、检查范围、结果、证据和限制，不能仅凭 build 成功或页面能打开宣告产品验收通过。已有证据适用于当前版本时可以引用，无需机械重跑；未覆盖或失败的必需项如实标记待验证/未通过，继续修复或推进不受影响的已授权工作，不以请求用户验收代替验证。用户反馈随到随处理，未反馈不构成阻塞。

```text
阶段：P2 / E03–E04
预览：实际可访问 URL（远程时附访问方式）
身份：branch / commit / dirty 状态 / observed_at
环境：分支预览或集成 / fixture 或 live；数据保留方式
操作：1… 2… 3…
预期：应该看到的结果
已验证：相关检查及对应提交
待完成/限制：没有实现或尚未验证的部分
agent 验收：待验证 / 未通过 / 通过（执行者、时间、范围、结果及证据）
用户反馈（可选）：未收到 / 已收到及处理结果；不作为继续条件
```

技术 ready、产品 accepted、merged、deployed 分开记录；产品 accepted 标明实际验收者为开发 agent，不代表用户亲自确认。不需要新增一套复杂 GitHub 标签，现 `status:review` 可覆盖适用技术评审及 agent 产品验收，具体状态写验收卡。验收委托不代替 §3 的独立 review 或 hosted CI，也不扩大生产操作授权。

## 3. 验证与 review 按风险缩放

| 级别 | 示例 | 本地检查 / 合并前要求 |
| --- | --- | --- |
| R0 低风险 | 普通文档、文案、纯样式，无交互/权限/契约变化 | 主代理自查+相关检查；不强制独立 review；视觉有变化给预览 |
| R1 普通功能 | 已有契约内的任务页面、UI/API 改动、局部行为修复 | 主代理实现、相关行为/浏览器检查和 diff 自查；不强制独立 review；开发 agent 按 §2 验收用户新流程 |
| R2 高风险 | 改变数据身份/聚合正确性、并发/恢复保证、权限边界、破坏性 schema/数据迁移、生产发布机制 | 合并前一次聚焦风险的独立 review，契约/故障恢复证据，完整适用 gate，风险说明和已授权范围核对 |

独立评审集中在高风险改动合并前，不作为开发、预览或创建 Draft PR 的前置条件。维护者于 2026-09-16 已长期授权：R2 可自动启动一个只读 reviewer，对本次 diff 的实际风险完成一次聚焦评审，无需再次询问；已有独立人类评审时不重复启动。reviewer 不修改代码、不继续委派，修复由主代理完成。普通开发仍单代理；双轴或多个 reviewer 仅在用户明确要求时启用，不自动调用双轴 `code-review` 技能。

review 发现需修复或有证据回应；后续只复查受影响发现和行为，出现新的实质风险才扩大范围。不为每次编辑或 commit 重启完整评审。普通“用户可见行为改变”、修改高风险模块内的文案或仅引用高风险概念，不自动升级为 R2；按实际改变的保证判断，PR 拆小也不能隐藏 R2 风险。

治理文档按规则的实际影响分级：勘误/说明整理可为 R0，已获授权的开发流程调整可为 R1；改变权限、生产发布、数据保护等高风险边界的规则仍为 R2。纯文档变更的本地适用检查为文档一致性、链接和规则冲突检查，不因 R2 标签机械运行与修改无关的 provider/浏览器全套验收。本次获批流程调整按 R1 自查，保留高风险独立 review、CI、产品验收和生产授权。

所有合并仍需 protected-main、当前待合入提交的 hosted `verify` 成功和可合并状态。已有权限/功能授权持续有效；只有新范围或未覆盖的高风险/生产动作需要补充确认。产品整体完成与生产激活是独立检查点，不需要积累一个巨大 PR，也不能默认每个小 PR 都发布。

开发中执行与变化有关的检查，不要求每个 commit 本地重跑全量。R2 跨模块和版本候选执行 `bash scripts/ci.sh all`；正式版本保留 Python 3.10/3.11/3.12、Node 22 和服务/浏览器验证。

### 检查入口与证据复用

首次进入 worktree 或依赖/锁文件变化时执行 `bash scripts/prepare-dev.sh backend|web|all`。默认后端创建 Python 3.11 `.venv` 并按对应约束安装；可用 `DATACENTER_PYTHON` 指向已有受支持环境。Web 显式执行一次 `npm ci --include=dev`。浏览器首次另装 `npx --prefix webui playwright install chromium`，主机依赖见下节。检查命令只验证，不重新安装依赖。

| 改动 | 命令 | 证据范围 |
| --- | --- | --- |
| 文档 | `bash scripts/ci.sh docs` | 状态字段、变更文档本地链接/锚点、秘密扫描、diff 格式；不需后端依赖 |
| 后端局部 | `bash scripts/ci.sh backend-fast backend/tests/test_dataset_center.py` | Ruff + 指定 pytest 文件/`::test_name`，必须显式选择测试 |
| 前端局部 | `bash scripts/ci.sh web-fast /datasets /tasks` | 类型/构建 + 独立 fixture 预览中指定路由的登录、直达/刷新、桌面/移动端布局及脚本错误检查 |
| 后端全量 | `bash scripts/ci.sh backend` | 后端原完整检查集（文档检查独立为 docs） |
| Web 全量 | `bash scripts/ci.sh web` | 构建、预览隔离、完整浏览器用户旅程、服务恢复 |
| 跨模块/候选 | `bash scripts/ci.sh all` | 文档、后端、Web 全量检查 |

`web-fast` 是路由 smoke，不代替本次业务操作的产品验收；新写入/调度流程仍须完成对应 E/PV/DS 操作及读回。`docs` 默认检查 HEAD 后的工作区变更；比较已提交的改动时设置 `DATACENTER_DOCS_BASE=<基线 commit>`。脚本不判断自然语言规范是否互相矛盾，规则冲突仍由主代理自查。

每次检查输出唯一 `acceptance-receipts/ci/<scope>-<时间>-<id>.json`，记录源码 commit/dirty/指纹、环境摘要、每项命令/结果/耗时及失败位置；PR、验收卡和路线图引用此记录，不重复抄测试清单。运行过程中源码或环境变化会将结果标记失败，防止把不同版本混成一次通过。`DATACENTER_CI_RECEIPT` 可另指定稳定输出名，唯一历史副本仍保留。

本地可显式追加 `--reuse-from <receipt>`，仅对源码身份、检查范围/命令、依赖版本与环境摘要完全匹配且所有检查通过的记录复用；不匹配就重新执行。当前采用整个工作区的保守指纹，任何源码改动都会使旧记录失效，不自动推断跨版本可复用范围。复用仅证明所列检查，不证明外部服务、实时源或持久预览仍健康；这些按本次现场重新验证。Hosted CI 禁止复用旧记录，始终执行当前提交的适用检查。

### Hosted CI 与发布触发

PR 更新运行一次 Checks；main push 保留合并后 Checks；未开 PR 的分支可用 `workflow_dispatch` 手动验证。纯 Markdown PR 在明确路径选择后只运行 docs；可执行/符号链接文档、代码、依赖、CI/共享工具、未知路径或空 diff 均跑完整矩阵。main push 和手动验证始终完整。`verify` 要求计划和 docs 成功，且后端/Web 为计划预期的 success 或明确不适用的 skipped；失败、取消或意外跳过一律拒绝。PR 测试身份以实际 checkout 的 commit（可能是 GitHub 合成 merge commit）和对应 PR head 映射核对，不以同分支旧 run 代替。

Release 仅响应 `v*` tag push 或手动指定已存在的版本 tag，验证 annotated tag、protected-main 可达性与 commit 对应的 verify。普通 main push 不触发发布，也不回退到固定旧版本。此触发规则不授权生产激活。

代码或行为变更后检查证据必须对应当前版本；UI 体验验收受影响时更新预览并由开发 agent 重验受影响部分，不复用旧行为的通过结论。网络故障/源延迟和 fixture 结果分别报告；默认 CI 不联网访问真实 provider。

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

历史 P1/P2/P2.1 预览的地址、命令与验收事实已移至[预览历史记录](integration/preview-history-20260916.md)。当前阶段以路线图为准，实际地址通过 status/card 读回，不从历史记录推断当前健康状态。

访问卡可直接生成，命令只读取现有预览，不重启、不重置数据：

```bash
bash scripts/dev-preview.sh card --id p21-eurusd-dataset --base /home/quant/repos/.preview
bash scripts/dev-preview.sh card --id p21-eurusd-dataset --base /home/quant/repos/.preview \
  --ssh-target user@host --receipt acceptance-receipts/ci/<本次记录>.json
```

卡片自动包含地址、运行代码身份、现场匹配状态、模式、进程、有效派发、保留路径与可选 SSH 转发命令。开发 agent 只补业务操作步骤、预期和限制。停止或身份不匹配时 card 返回非零，仍输出诊断；引用 receipt 时明确显示结果与代码是否匹配，不自动把健康状态等同产品验收。

运行进程仍依赖启动它的代码 worktree 和 Python 环境。删除或替换该 worktree 前先用上述外置 base 停止预览；在新 worktree 安装锁定依赖后，再以相同 id/base 和 `--update` 重启，原持久数据会继续使用。地址以 `status` 的实际输出为准；若端口或运行主机改变，应同步更新本节与根 AGENTS 路由提示。

预览交付必须运行 API/worker/scheduler/Vite，而非只有静态页；模拟内容显著标识。stop 保留数据；浏览器测试不销毁用户预览；更换版本要说明。登录凭据通过适当本地交付方式提供，不写入公共验收卡。

默认预览只允许 fixture connector、关闭告警外发，并为 API、worker、scheduler、auth、canonical、ledger、evidence、backup 和日志提供独立根。页面顶部显示预览身份、模拟数据和 scheduler 心跳。各阶段已验证项与未完成范围见路线图，交付卡必须明确本次限制。

## 5. 文档与发布

spec 描述要求，plan 记录排期，guide 记录已可执行操作，research 保留来源。状态至少区分 approved/implemented/accepted；`排期：paused` 与实现状态独立。对旧 spec 局部取代时在文首链接新规范并列出范围，不改历史 receipt。

当前事实带 observed_at；源码、运行部署、产品验收分别标示，产品验收注明 agent 或用户实际执行者。生产身份需要读回，未读回就保留历史快照说明。发布时读 release checklist/runbook；开发预览不套生产激活程序。
