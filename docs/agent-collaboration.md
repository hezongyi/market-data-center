# Agent 协作约定（issue 认领与多 agent 参与）

本文件规定**多个 agent（以及人类）如何在本仓库里提出、讨论、认领和关闭 issue**，以及 agent 提交的 PR 需要满足什么门槛。
`AGENTS.md` 是本仓库的总入口；本文件是它在"协作"方向上的展开，两者冲突时以 `AGENTS.md` 为准。

适用对象：任何通过 `gh` / GitHub API 在本仓库读写 issue 与 PR 的自动化参与者（以下统称 "agent"），以及与之协作的人类维护者。

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
| `status:review` | PR 已开，等待门禁与人类批准 | 认领者 |
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
- 你能跑通本仓库的门禁（`bash scripts/ci.sh all`；浏览器相关需按 `AGENTS.md` 配置 Playwright 环境变量）。
- 你**不会**顺手改与本 issue 无关的东西；发现额外问题请**另开 issue**并标注来源。

---

## 4. 从 issue 到合并

| 环节 | 要求 |
|---|---|
| 分支 | 从最新 `origin/main` 拉出，命名 `fix/<issue号>-<slug>` 或 `feat/...` / `docs/...`；**永不直接推 `main`** |
| 提交 | 提交信息说明"改了什么 + 为什么"；引用 issue 号 |
| PR 描述 | 必须包含：动机、改动点、**`Closes #<issue>`**、验证方式（本地门禁输出摘要）、影响面（是否涉及契约/版本/发布）、存疑点 |
| 门禁 | 本地 `bash scripts/ci.sh all` 通过；PR head 上 `verify` 成功；`mergeable_state = clean` |
| 复审 | **必须有一次 review**（可以是另一个 agent 或人类），review 意见要么修要么回复说明 |
| 批准 | 采用两级门禁：小 PR 通过自动门禁和 review 即可合并；Spec 完成型大 PR，以及涉及认证、数据迁移、核心契约或生产行为的 PR，必须先提交风险汇总并取得维护者明确口头同意，再在 PR 留言记录确认范围、确认人和时间 |
| 合并 | 小 PR 在门禁满足后可合并；大 PR 在取得上述确认后合并；合并方式与仓库既有 PR 一致；合并后删除分支 |
| 发布 | 仅当改动涉及生产行为时按 `docs/release-checklist.md` 走版本收口与部署；**纯文档/测试改动不需要发布** |

### 两级门禁

- **小 PR**：范围局部、可回滚，不改变认证边界、数据迁移、核心 API/数据契约或生产发布行为。要求本地/ hosted 门禁通过、至少一次 review，并在 PR 描述中注明所属 spec、阶段和验证结果；无需逐次请求维护者口头确认。
- **大 PR**：完成一个 spec 的整体交付，或触及认证、数据迁移、核心契约、生产行为的任何变更。合并前必须向维护者汇总完成范围、未完成项、风险、验证证据和“是否建议合并”，取得明确口头同意，并将确认记录到 PR 评论。
- **生产发布**：无论 PR 大小，均须按发布清单单独取得发布确认并保留 receipt。
- 任一 reviewer 或维护者可以要求将小 PR 升级为大 PR；拆分 PR 不得规避大 PR 门禁。

反模式（会被退回）：

- 用"门禁绿了"代替 review；用"我测过了"代替可复现的证据。
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

本协议本身也是一份文档：**任何 agent 都可以提 PR 修改它**，但同样需要人类批准。修改时必须说明"哪条规则导致了什么问题"，而不是只改措辞。
