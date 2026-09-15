# Agent 协作约定（issue 认领与多 agent 参与）

本文件规定**多个 agent（以及人类）如何在本仓库里提出、讨论、认领和关闭 issue**，以及 agent 提交的 PR 需要满足什么门槛。
`AGENTS.md` 是本仓库的总入口；本文件是它在"协作"方向上的展开，两者冲突时以 `AGENTS.md` 为准。

适用对象：任何通过 `gh` / GitHub API 在本仓库读写 issue 与 PR 的自动化参与者（以下统称 "agent"），以及与之协作的人类维护者。

2026-09-15 修订：当前范围由[EURUSD 产品规范](specs/2026-09-15-eurusd-first-product-baseline.md)决定，交付与风险规则由[开发指南](development-guide.md)决定。直接委派的本地任务无需先建 issue；本协议的认领与来源约定适用于共享 issue 和已授权的 GitHub 协作，不自行授权外部发言。

---

## 1. 身份与来源标注（当前方案：A）

GitHub 的 issue **作者不可更改**，本仓库目前也**没有**专用 bot 账号或 GitHub App，因此：

- agent 通过维护者 `hezongyi` 的 `gh` 凭据代发，**author 显示为人类账号**；
- 归属靠**三件套**声明，缺一不可：

| 标记 | 形式 | 作用 |
|---|---|---|
| 标签 `agent-reported` | GitHub label | 可被 API/label 过滤器一键筛出 |
| 可见脚注 | 正文末尾 `<sub>` 段落 | 人类阅读时立刻知道来源与基线 |
| 机读元数据块 | 正文末尾 HTML 注释 `<!-- agent-report ... -->` | agent 用 API 解析（渲染时不可见） |

机读块字段（`scripts/agent_claim.py annotate` 会生成，缺省值可覆盖）：

```text
<!-- agent-report
reporter_kind: ai-agent
model: <model id>
harness: <harness id>
reported_at: <ISO-8601 UTC>
submitted_via: gh-cli
credential_owner: <github login that owns the token>
baseline_repo_version: <e.g. 0.4.1>
baseline_commit: <40-hex>
baseline_deployment_id: <deployment id, 可选>
source: <这次上报的来源：PR/走查/监控 等>
-->
```

**边界（必读）**：这是"声明式归属"，不是密码学证明 —— 任何有写权限的人都能改这段注释。它的价值是**可发现、可过滤、可追责到会话**，不是防篡改。
若将来需要真正区分身份（多 agent 各自署名、按 author 过滤/指派），升级路径是：专用 machine account + fine-grained PAT（`issues:write`、`pull_requests:write`、`contents:read`），或 GitHub App + 短期 installation token。**升级时不要新建重复 issue**，在既有 issue 里补充说明即可。

### 上报者必须遵守

1. 只报告**能复现或有源码/接口证据**的问题；每条问题的关键论断附 `文件:行` 或实测输出。
2. 报告里写清**核对基线**（版本、commit、deployment id）—— 没有基线的 issue 会被当作无法验证而退回 `status:triage`。
3. **不夸大**：把"没找到"写成"没找到"，不要写成"不存在"；不确定的地方显式标注。
4. 若判定为有意设计，在正文里给出**退出路径**（"若为有意设计请关闭并说明"），避免制造噪音。

---

## 2. 标签体系（状态机）

状态标签**互斥**：任一时刻一个 issue 最多一个 `status:*`。

```
status:triage ──► status:accepted ──► status:claimed ──► status:in-progress ──► status:review ──► status:done
                       │                   │                    │
                       │                   └──── 租约到期/放弃 ─┴──► status:accepted
                       └──► wontfix / status:blocked（附理由）
```

| 标签 | 含义 | 谁可以设置 |
|---|---|---|
| `status:triage` | 新上报，尚未评估（默认给新 issue） | 任何人 |
| `status:accepted` | 已确认是有效问题，**可被认领** | 维护者，或 agent 在给出复现证据后 |
| `agent-claimable` | 显式声明"欢迎任意 agent 认领"（与 `status:accepted` 同时使用） | 维护者 |
| `status:claimed` | 已被某个 agent 认领（有 `/claim` 评论） | 认领者 |
| `status:in-progress` | 已开工（有分支/PR 链接） | 认领者 |
| `status:review` | PR 已开，技术评审/产品反馈状态在验收卡分别记录 | 认领者 |
| `status:blocked` | 外部条件阻塞（必须写明阻塞点与解除条件） | 认领者或维护者 |
| `status:done` | 已合并/已关闭并验证 | 合并者 |
| `wontfix`（复用 GitHub 默认标签，**不是** `status:*`） | 明确不做，需说明理由；因不属于状态机，用 `gh issue close` + 该标签处理 | 维护者 |
| `priority:p0/p1/p2` | 紧急度（p0 仅用于生产可用性/数据正确性） | 维护者 |

类型标签沿用 GitHub 默认集（`bug`、`enhancement`、`documentation`、`question`…）。`agent-reported` 与类型标签**可以共存**。

---

## 3. 认领协议（解决并发）

GitHub 没有 compare-and-swap，所以认领靠"**声明 + 复核 + 租约**"三件套。推荐直接用脚本（见 §6），手工执行时按同样步骤：

1. **只认领**带 `status:accepted` 或 `agent-claimable` 的 issue；`status:claimed`/`in-progress`/`review` 的默认视作已被占用。
2. 发一条认领评论（`<!-- agent-claim -->` 标记 + 字段）：

   ```text
   /claim
   <!-- agent-claim
   agent: <你的标识，建议 harness/model/会话>
   claimed_at: <ISO-8601 UTC>
   plan: <两三步的实现计划>
   expected_artifact: <PR / 分析报告 / 复现脚本>
   eta: <ISO-8601 UTC>
   -->
   ```

3. 打 `status:claimed`（并移除 `status:accepted`）。
4. **立刻重读 issue**：若发现存在**更早的**、未过期的他人 `/claim`，则主动退让 —— 回复一条说明、撤掉自己的标签，不要继续开工。
5. **租约 24h**：认领后 24 小时内至少发一条进度评论（`/progress`），否则任何 agent 可把状态退回 `status:accepted`，并在评论里 @ 原认领者说明回收理由。
6. **一个 issue 一个 owner**。需要协作时由 owner 在评论里 @ 其它 agent 并说明分工，而不是各自打标签。

### 认领前必须确认的

- 问题在当前 `origin/main` 上**仍然存在**（基线漂移会让旧 issue 失效；先复现再认领）。
- 你已检查相关工具与针对性验证可执行；不要求为认领先跑一次完整 gate。浏览器相关按 `AGENTS.md` 配置 Playwright 环境变量。
- 你**不会**顺手改与本 issue 无关的东西；相关修正在本任务处理，额外范围先记录，获授权后另开 issue。

---

## 4. 从 issue 到合并

| 环节 | 要求 |
|---|---|
| 分支 | 从最新 `origin/main` 拉出，命名 `fix/<issue号>-<slug>` 或 `feat/...` / `docs/...`；**永不直接推 `main`** |
| 提交 | 说明改了什么及原因；有 issue 时引用，没有时引用切片/验收编号 |
| PR 描述 | 动机、行为变化、验收编号、风险级别、相关验证、限制；只有实际关闭整个 issue 才用 `Closes`，部分实现用 `Refs` |
| 预览 | Draft PR 阶段即可给 URL、commit、数据模式和操作步骤；无需先完成 review 或发布级门禁 |
| 门禁 | 本地相关检查；R2/候选运行完整适用 gate；当前提交 hosted `verify` 成功，`mergeable_state = clean` |
| 复审 | R0 可自查；R1 一次独立 review；R2 深入 review；意见修复或给证据回应，复查聚焦受影响部分 |
| 产品验收 | 用户可见新流程合并前提供阶段验收；整体功能在集成环境验收，不以技术 review 代替 |
| 批准 | 核对已有授权；未覆盖的高风险范围需要风险说明与明确确认，保留人/时间/范围；不重复索取同一批准 |
| 合并 | 满足对应门槛后按既有方式合并；累计功能采用小 PR，不强制巨大收口 PR |
| 发布 | 预览更新、合并、tag、生产激活分别交付。普通功能并入验收版本，生产操作按 release checklist 与已授权范围执行；纯文档/测试不需发布 |

### 风险分级

R0/R1/R2 的定义与验证矩阵以[开发指南 §3](development-guide.md#3-验证与-review-按风险缩放)为准。低风险不含治理规则；数据身份、聚合、并发/恢复、权限、破坏性迁移、发布机制属于高风险。功能变化本身不自动成为高风险。

reviewer 可根据具体影响升级风险，拆分 PR 不得规避高风险验证。生产操作另行核对授权与 receipt，设计批准不等于部署批准。

反模式（会被退回）：

- 用“门禁绿了”代替必要的独立 review 或用户验收；用“我测过了”代替可复现证据。
- 在同一个 PR 里混合多个不相关 issue 的改动。
- 为了让测试变绿而放宽断言、跳过用例或改测试基线（必须说明为什么原断言是错的）。
- 直接改生产环境（`stage`/`activate`/`rollback`）而没有对应 tag 与 receipt。

---

## 5. 讨论规范

- **讨论留在 issue，实现细节留在 PR**。issue 里达成结论后，认领者要在 issue 里写一句**结论摘要**，避免结论只存在于 PR 评论或某个 agent 的上下文里。
- 分歧用"**证据 + 复现**"表达：给出命令、输入、实际输出、期望输出。禁止只有观点的长贴。
- 反对意见要给出**可验证的替代方案**；不能验证的担忧写成"风险提示"并说明触发条件。
- 关闭 issue：`status:done` 时必须附**验证证据**（PR 链接 + 门禁结果 + 必要时生产核验）；`wontfix` 必须附理由。

---

## 6. 工具：`scripts/agent_claim.py`

把上面的协议做成可执行命令（依赖已登录的 `gh`；不读取、不打印任何凭据）：

```bash
python scripts/agent_claim.py list --claimable          # 列出可认领 issue
python scripts/agent_claim.py claim 82 --agent my-id \
    --plan "定位 handoff 丢失点; 补草稿; 加验收断言" \
    --artifact "PR" --eta 2026-09-14T09:00:00Z
python scripts/agent_claim.py progress 82 --note "分支已开，正在补断言"
python scripts/agent_claim.py status 82 status:review
python scripts/agent_claim.py release 82 --reason "无法复现，退回"
python scripts/agent_claim.py annotate 90 --model deepseek-flash --source "监控复核"
```

行为要点：

- `claim` 在写入前后各读一次 issue，发现**更早且未过期**的他人认领时**自动退让**（撤销自己的评论与标签，退出码 4）。
- `--assign` 可选：把 issue 指派给 token 属主（非 collaborator 会失败，脚本降级为仅打标签并提示）。
- `annotate` 幂等：正文已含 `agent-report` 块时不做任何修改。
- 所有命令支持 `--json`，便于其它 agent 直接消费。

---

## 7. 修改本协议

本协议修改属于 R2 治理变更，需要说明哪条规则导致什么问题并核对维护者授权。本次 2026-09-15 修订由维护者“认可以上全部提议”授权：取消每次本地全量重复，修正生产行为分类过宽，提前可操作预览，保留风险相称的独立 review 与生产授权。后续不要为同一已批准方向重复询问；未经授权的新方向另行说明。
