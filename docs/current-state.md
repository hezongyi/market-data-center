# Market Data Center 当前状态

更新时间：2026-09-13。本文描述当前生产事实；历史 receipt、旧 deployment 和阶段性计划保留原文，不代表当前状态。

## 生产运行

| 项目 | 当前事实 | 证据 |
| --- | --- | --- |
| 当前 deployment | `a8f6e3d616e9-257b2f31`（`software_version=0.3.2`） | deployment activation receipt |
| API/worker | systemd active；readiness=ready | `/api/v1/health/ready` |
| 容量/队列 | `capacity_status=ok`、queue=0 | `/api/v1/metrics` |
| Dukascopy raw | `provider_bars`、1m、BID-only | `2026-09-11-dukascopy-1m-bid-rollout.md` |
| Dukascopy derived | 5m/15m/30m/1h/4h/1d recipes；完整历史受 coverage 约束 | `dukascopy_derived_multiperiod_acceptance_v3` |
| Dukascopy maintenance | macro-market-lab systemd service 调用 Data Center runner | `macro_market_lab_maintenance_ownership_cutover` |

## 版本与发布基线

| 项目 | 当前事实 | 证据 |
| --- | --- | --- |
| 源码版本 | `0.3.2`；`backend/pyproject.toml`、`webui/package.json` 与 `data_center.__version__` 三者一致 | release contract test |
| 最新发布标签 | `v0.3.2` → `a8f6e3d616e92f78a50ce9ec0800d0eb324836b9`（annotated、不可变） | [GitHub release](https://github.com/hezongyi/market-data-center/releases/tag/v0.3.2) + `release-receipt.json` |
| 生产 deployment source commit | `a8f6e3d616e92f78a50ce9ec0800d0eb324836b9`（= `v0.3.2`），是 protected `main` 的祖先 | active deployment manifest |
| 基线规则 | 生产 deployment 只能由 commit-scoped `verify` 成功的 protected-main commit 创建；release 标签只打在该 commit 上且不可移动 | `docs/release-checklist.md` |

生产 deployment 现与 `v0.3.2` 发布基线一致。2026-09-13 的 activation 演练保留了完整回执：`v0.3.1` 的 stage/activate、注入候选 readiness 失败后的自动恢复（`recovered_deployment_id=b5d1bc95681b-610c867f`，canonical 与 ledger 哈希未变）、回滚到 `4016a992669d-b0be2ea0` 后再次前滚、`v0.3.1` 的 60 分钟 monitor soak（33 次运行、无重叠、身份稳定），以及 `v0.3.2` 的 stage/activate。升级只允许走 immutable activation 流程，不得手工改动 systemd unit 或依赖。

monitor timer 配置为 `OnUnitInactiveSec=60s`，但实测节奏为约 120s（systemd 默认 `AccuracySec=1min` 的合并效应），即告警分辨率实际减半；这是配置事实，不是故障。

## Consumer / ownership 矩阵

| Consumer/数据域 | 当前路径 | 状态 | 回滚/边界 |
| --- | --- | --- | --- |
| macro-market-lab bar preview/query | Data Center HTTP adapter | 已默认切换 | `MACRO_MARKET_USE_DATA_CENTER_BARS=0` |
| macro-market-lab Dukascopy raw/derived maintenance | Data Center runner | 已切换 | `MARKETLAB_MARKET_BARS_BACKEND=legacy` |
| yfinance DXY/SPY/QQQ/TLT macro-daily | macro-market-lab legacy workflow | 未迁移 | 独立数据域 |
| economic PIT/current consumers | 现有 flag/legacy 路径 | 未完成全量切换 | 必须先完成 PIT parity |
| ASK/MID | 未采集 | 第一阶段非目标 | 需独立 identity/API/spec |

## 状态语义

`implemented` 表示代码/测试存在；`deployed` 表示进入 immutable release；`accepted` 表示有真实 receipt；`default cutover` 表示默认走新路径且有回滚；`not migrated` 表示明确仍走旧路径。

## 未完成事项

1. yfinance macro-daily 数据域迁移。
2. economic PIT/current consumer 全量切换。
3. Dukascopy 历史 provider gap 不补造；高周期完整历史覆盖不作为已完成条件。
4. 1m maintenance 的 `failed` 语义已收敛：provider 无数据（`ProviderGapError`）与 provider 覆盖不完整（`coverage_not_ready`）均记为 `degraded`，cooldown 抑制同样为 `degraded`，结构性质量失败仍为 `failed`。生产实测：2026-09-13 03:11:52 的运行 `result=pass`、`failed_target_count=0`、`degraded_target_count=1`（BTCUSD 17 个 degraded 窗口），systemd 单元 `Result=success`；数据始终 `quality_status=pass`，未被伪造或丢失。
