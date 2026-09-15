# 调度器接管补充验收索引

规范：[调度器接管修复与验收补充规范](specs/2026-09-15-scheduler-takeover-remediation-and-acceptance.md)
跟踪：issue #120
更新时间：2026-09-15 03:10 UTC

本页只索引已取得的证据，不把实现、计划或等待中的观察记为通过。生产 evidence root 为
`$HOME/market_lake/evidence/data-center`；路径以该 root 为基准，避免提交机器专属绝对路径。

## 身份与现场保护

| 项目 | 事实 | 证据 |
| --- | --- | --- |
| 故障 deployment | `aca73a045275-c6470772` / `0.6.1` / `aca73a045275908b4fd710df777f563fef673b90` | active manifest、readiness、activation receipt |
| 首轮 | manual execution `9b30a301-deaa-4d14-9e8e-6cc78dab24d7` → dead letter `a497384b-ddc2-4b79-b16c-6b99ca40d379` | production ledger；14 根预期、13 根返回、缺 22:19Z |
| 自动续轮 | scheduled execution `be124b50-8128-4d9f-aaa5-322cf253fb52` → dead letter `04e82d5f-ab53-4ca2-b5da-d794d8dfd27c` | production ledger；证明 fixed-delay 自动续轮发生，也复现同类故障 |
| 暂停 | 2026-09-15T01:51:39.190017Z `pause_dispatch`；随后有效 dispatch=false、queued=0、running=0 | audited operations API response、scheduler/metrics readback |
| legacy | 两个 timer disabled/inactive；旧派生 service 历史 failed | systemd readback；尚未证明回退可用 |

## TA 状态

| TA | 状态 | 当前证据 / 下一证据 |
| --- | --- | --- |
| TA01 | implemented，待 hosted/生产 | 隔离真实 worker 回归覆盖 14 期望/13 返回、一次尝试、immutable failed run、execution degraded、精确 1m gap、无 dead letter；mixed/structural 仍失败；03:10Z 完整门禁通过 |
| TA02 | implemented，待 hosted/生产 | exact debt 使用 schema-5 兼容的索引 backing table；持久化 next_review/policy，due 分页跳过 cooling 首页，崩溃边界可重放且修复债务不复活；03:10Z 完整门禁通过 |
| TA03 | 部分历史证据 | v0.6.1 manual 首轮和 scheduled 次轮时间链已存在，但均失败；候选版本需重验成功/降级轮次及暂停恢复 |
| TA04 | 待验证 | 原 shadow 23:09:24–01:28:15 共 311 tick、约 2h19m，只作为部分证据；需新的连续 ≥4h 有效对照 |
| TA05 | 待验证 | EURUSD raw-only 候选真实发布、manifest、HTTP readback、自动续轮 |
| TA06 | 待验证 | 单独批准的 crypto 与 1m→5m 固定输入/lineage/读回 |
| TA07 | 进行中 | 新派发已暂停且队列收口；仍需验证 legacy runner 身份、隔离回退演练、生产回退/前向 receipt |
| TA08 | 进行中 | v0.6.1 tag/activation 存在；Release run 34917000586 失败且无 release asset；候选已修 Release/verify 竞态，待 PR/发布/激活 |
| TA09 | 进行中 | spec、current-state、实施计划、runbook、release checklist 和本索引已在候选分支同步；待 PR/issue/最终索引 |
| TA10 | 待验证 | TA01–TA09 通过后形成具体批次清单与 go/no-go；尚未扩面 |

## 隔离验证命令

以下命令不访问真实 provider，使用临时 ledger/canonical/evidence：

```bash
PYTHONPATH=backend/src <venv>/bin/python -m pytest -q \
  backend/tests/test_ledger_reliability.py \
  backend/tests/test_maintenance_runner.py \
  backend/tests/test_production_window_planning.py \
  backend/tests/test_production_dispatch.py \
  backend/tests/test_session_closed_windows.py \
  backend/tests/test_release_sustainability_contracts.py

env -u NODE_ENV DATACENTER_PYTHON=<venv>/bin/python bash scripts/ci.sh all
```

每次记录当前 HEAD、退出码、测试数量和 CI receipt。候选实现、protected-main、tag/deployment 是不同身份，
不得互相借用结果。

## 候选工作树验证

- UTC：2026-09-15 03:06–03:10；分支 `docs/scheduler-takeover-acceptance-20260915`；工作树随后固化为
  候选提交 `7c1c39e`（基于 `aca73a045275908b4fd710df777f563fef673b90`）。本地结果不冒充 hosted commit evidence。
- `env -u NODE_ENV DATACENTER_PYTHON=<venv>/bin/python bash scripts/ci.sh all`：exit 0；backend
  `475 passed, 5 skipped`；Ruff、依赖/兼容、secret、operations/capacity、Web build、隔离 browser
  acceptance（desktop/mobile）和 service restart acceptance 全部 pass。
- 双轴评审：Standards 与 Spec 最终复审均无剩余可证实问题；期间发现并修复错误 gap 分类、非 gap
  自动重提、无界/N+1 debt 扫描、崩溃窗口、债务截断/饥饿、回滚不兼容和发布文档时态问题。
