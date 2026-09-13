---
name: Agent-reported problem
about: 由 agent（或人类）提交的、带证据与基线的问题报告
title: "[<area>] <一句话结论>"
labels: agent-reported
---

<!--
由 agent 提交时请保留 `agent-reported` 标签，并用 `python scripts/agent_claim.py annotate <number> ...`
补上 `agent-report` 机读元数据块。协议见 docs/agent-collaboration.md。
人类提交时可删除本注释与"报告来源"一节。
-->

## 现象

<发生了什么，用户在界面上/调用接口时看到什么。一段话，不要先给结论。>

## 证据

<`文件:行` 引用、复现命令与实际输出、接口返回片段。关键论断必须有证据。>

```text
<命令与实际输出>
```

## 基线

- 版本 / tag：
- commit：
- deployment id（涉及生产时）：

## 影响

<谁会受影响、后果是什么、有没有数据正确性或可用性风险。>

## 建议

<按成本排序的修复建议；如果不确定，写清可选的几种方向。>

## 验收建议

<怎样才算修好：可执行的断言、测试或浏览器验收步骤。>

## 若为有意设计

<"若判定为有意设计，请关闭并说明理由。" —— 给出退出路径，避免制造噪音。>
