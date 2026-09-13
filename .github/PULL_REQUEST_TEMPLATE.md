<!--
agent 提交的 PR 请填满下列各节；协议见 docs/agent-collaboration.md。
注意：agent 提交的 PR 需要人类维护者批准后才能合并，CI 通过不等于可以合并。
-->

## 动机

<解决哪个 issue / 哪个已观测到的问题。>

Closes #

## 改动点

<按文件或按行为列出；说明为什么这么做，而不是只列做了什么。>

## 验证

- [ ] 本地 `bash scripts/ci.sh all` 通过（粘贴关键输出摘要）
- [ ] PR head 上 `verify` 通过（确认 workflow `head_sha` == 本 PR head）
- [ ] 涉及界面时：浏览器验收覆盖（不是手工点一遍）
- [ ] 涉及数据/契约时：契约测试或 acceptance 覆盖

```text
<门禁输出摘要>
```

## 影响面

- 是否涉及 API 契约：
- 是否涉及版本号 / 发布 / 生产部署：
- 数据写入或历史 receipt 影响：

## 存疑点与已知限制

<没能验证的部分、权衡过的替代方案。写"没验证"而不是假装验证过。>

## Agent 声明

- 提交者：<!-- agent id / harness / model，或 "human" -->
- 是否由 agent 主要撰写：yes / no
- 复审者：<!-- 另一个 agent 或人类；写明复审了什么 -->
