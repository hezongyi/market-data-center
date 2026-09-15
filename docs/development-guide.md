# 开发与阶段验收指南

生效依据：2026-09-15 维护者确认的 EURUSD 优先交付决策。适用于 agent 与维护者共同开发；产品操作说明仍放 `webui-user-guide.md`。

## 1. 先确定当前工作

读取 [当前产品规范](specs/2026-09-15-eurusd-first-product-baseline.md)与[路线图](plans/2026-09-15-eurusd-first-implementation.md)，UI 另读 `webui/AGENTS.md`，预览另读[预览规范](specs/2026-09-15-isolated-preview-environment.md)。旧 spec 的历史状态不代表当前排期，未列入本轮的旧工作默认暂停。

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

预览交付必须运行 API/worker/scheduler/Vite，而非只有静态页；模拟内容显著标识。stop 保留数据；浏览器测试不销毁用户预览；更换版本要说明。登录凭据通过适当本地交付方式提供，不写入公共验收卡。

默认预览只允许 fixture connector、关闭告警外发，并为 API、worker、scheduler、auth、canonical、ledger、evidence、backup 和日志提供独立根。页面顶部显示预览身份、模拟数据和 scheduler 心跳。P0 不完成 P1 的路由/shadcn 页面，也不完成 P2/P3 的完整任务数据闭环或 P4 的第二任务旅程；这些限制必须继续显示在阶段验收卡中。

## 5. 文档与发布

spec 描述要求，plan 记录排期，guide 记录已可执行操作，research 保留来源。状态至少区分 approved/implemented/accepted；`排期：paused` 与实现状态独立。对旧 spec 局部取代时在文首链接新规范并列出范围，不改历史 receipt。

当前事实带 observed_at；源码、运行部署、用户接受分别标示。生产身份需要读回，未读回就保留历史快照说明。发布时读 release checklist/runbook；开发预览不套生产激活程序。
