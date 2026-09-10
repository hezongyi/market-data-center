# Query Contract and Scalability Specification

日期：2026-09-10  
状态：completed  
前置 spec：`2026-09-10-economic-pit-and-operations`

## 目标

在保持现有 `/api/v1` envelope、PIT 语义和 immutable publication 不变量的前提下，修复 economic 混合 schema 版本识别，并把查询路径改造成版本可追踪、结果有界、可稳定分页且不会在每次读取时重复执行全量发布校验的深模块。

本阶段的核心 seam 位于 catalog/query module：调用方只提供 selector、查询模式、时间范围和分页参数；module 负责 manifest snapshot、schema provenance、current/PIT 去重、稳定排序、分页游标和错误语义。API、Web UI 与 consumer 不得各自推断这些规则。

## 已知问题

1. Economic API 当前通过结果行是否包含 `source` 字段推断 v1/v2。`read_parquet(..., union_by_name=true)` 会给 legacy v1 行补齐 `source=null`，因此混读时可能错误报告为 v2。
2. `published_files()` 在每次查询时重新验证全部 manifest，并把全部 Parquet 行读入内存执行 schema/quality validation；查询层随后又通过 DuckDB 读取相同文件。
3. Bars 与 economic read endpoint 没有稳定分页或服务端结果上限，数据增长后会放大延迟、内存和响应体风险。
4. 如果分页期间发布了新 manifest，单纯按最后一行生成游标可能产生重复或遗漏；游标必须绑定一致的 catalog snapshot。

## 范围

### 1. Schema provenance

- Query module 必须从已发布 manifest 获取 schema version，不得根据字段存在、字段值或空值模式进行推断。
- Query result 的 metadata 至少包含 `schema_versions`，值为实际参与结果的完整 contract 名称，例如 `economic_observations.v1`。
- 为兼容现有 consumer，economic API 暂时保留 `economic_schema_version`：单版本返回 `v1`/`v2`，多版本返回 `mixed`，空结果返回 `unknown`。
- Mixed-schema 读取继续使用 union-by-name，但 schema provenance 与行内容解耦。
- Manifest、part 与结果之间必须能够追踪；不要求在每一行重复输出 schema version。

### 2. Catalog snapshot

- Catalog module 暴露一个小 interface：根据 dataset 与 selector 返回 immutable snapshot，包括 snapshot ID、已验证 manifest metadata、part 引用和 schema versions。
- Manifest 的完整 schema、hash、row count 和 quality validation 继续发生在 publication seam。
- 查询时只验证发布状态和 snapshot/index 一致性，不得再次把所有 part 完整读取为 Python records。
- 新 manifest 原子发布后必须形成新的 snapshot；已签发的分页游标继续绑定旧 snapshot，直至游标过期。
- 缺失、损坏或不兼容的 manifest 必须 fail closed，不得静默跳过后返回部分数据。
- 初始实现可以使用进程内 cache 和 manifest metadata index，不要求引入外部数据库或缓存服务。

### 3. Query page interface

Query module 对 bars 和 economic 暴露统一的 page result 概念：

- 输入：dataset selector、`start`、`end`、economic `mode/asof_ts`、`page_size`、opaque `cursor`。
- 输出：`rows`、`count`、`schema_versions`、`snapshot_id`、`next_cursor`。
- 使用显式分页时，`page_size` 默认 1,000、最大 10,000；非法值返回稳定的 validation error。
- Cursor 必须绑定 dataset、selector、mode、as-of、排序键和 snapshot ID；参数不匹配、被篡改或已过期时返回明确错误。
- Provider bars 按 `bar_ts` 升序；economic observations 按 `observation_date` 升序。相同业务键的 current/PIT 去重规则保持现有契约。
- 分页不得改变 PIT 可见性：任何页面都不能观察到 `asof_ts` 之后才可用的数据。

### 4. API 与 consumer 迁移

- `/api/v1/bars` 和 `/api/v1/economic/observations` 增加可选 `page_size` 与 `cursor`，envelope 保持 `data`、`meta`、`errors`。
- `meta` 增加 `next_cursor`、`snapshot_id`、`schema_versions` 和 `count`，属于 additive contract change。
- 第一阶段未传分页参数时保持现有完整结果行为，但记录超过默认 page size 的 structured warning。
- DataCenterClient、Web UI 和 `macro-market-lab` consumer 必须迁移为显式分页；全部迁移与 parity 通过后，另行决定是否在 API v2 中强制有界读取。
- Consumer 不得自行拼接或解析 cursor，也不得直接读取 manifest/index。

### 5. 性能与可观测性

- Metrics 增加 catalog snapshot refresh、query duration、rows scanned、rows returned、cache hit/miss 和 rejected oversized query 计数。
- 日志保留 `request_id`，并记录 dataset、selector hash、snapshot ID、query mode、page size 和 duration；不得记录 API key 或完整敏感 selector。
- 在验收数据集上，查询路径不得重复执行 publication-time 全量 row validation。
- 基准数据至少包含 1,000 个 immutable parts 和 1,000,000 行；在验收主机上，返回 1,000 行的 warm query P95 目标小于 1 秒，cold query 小于 3 秒，单请求额外 RSS 目标小于 512 MiB。

## 不变量

1. Query 只能读取通过 immutable manifest 发布的数据，不能观察 staging 或半发布 run。
2. Schema version 只来自受验证的 manifest，不从结果字段推断。
3. 同一个 cursor 的后续页面使用同一个 snapshot；新发布数据不得插入既有分页序列。
4. PIT 查询不得观察未来发布或未知发布时间的数据，availability lag 继续生效。
5. Query 优化不得修改 canonical part、manifest、receipt 或 terminal ledger 状态。
6. 新 metadata 必须是 additive；现有 consumer 在迁移完成前仍可读取原有 envelope。

## 实施阶段与门禁

### Q1：版本正确性

- Query result 传播 manifest schema versions。
- 修复 `economic_schema_version` 混读误报。
- 增加 v1-only、v2-only、mixed 和 empty result contract tests。

门禁：混合 fixture 返回 `schema_versions=[economic_observations.v1,economic_observations.v2]` 且兼容字段为 `mixed`；现有 PIT/current 测试全部通过。

### Q2：Catalog 深化

- 将 publication-time 深度校验与 query-time snapshot resolution 分离。
- 建立可失效的 manifest metadata index/cache。
- 增加损坏 manifest、新 manifest 发布和进程重启测试。

门禁：查询不再通过 Polars 完整读取每个 part 做重复校验；损坏 metadata 仍 fail closed；发布后新 snapshot 可见。

### Q3：稳定分页

- 实现 opaque snapshot-bound cursor。
- API、Python client 和 Web UI 支持分页。
- 验证跨页无重复、无遗漏以及参数不匹配错误。

门禁：分页期间并发发布新 manifest，旧 cursor 的完整结果仍与分页开始时 snapshot 的稳定 hash 一致。

### Q4：Consumer 与性能验收

- `macro-market-lab` 在固定窗口完成 paged/unpaged 双读 parity。
- 运行百万行/千 part 基准并记录 receipt。
- 将 query metrics 和超限告警加入 operations runbook。

门禁：row count、min/max 时间、稳定 output hash、PIT 可见性和错误语义一致；性能预算有包含 commit、环境、数据规模和结果的证据。

## 验收证据

- Mixed-schema API 回归测试和真实 legacy/current fixture receipt；
- Catalog snapshot publication、corruption 和 restart 测试；
- 分页期间并发 publication 的稳定性测试；
- DataCenterClient、Web UI 与 `macro-market-lab` pagination parity；
- 百万行/千 part benchmark receipt；
- 统一 CI 与受保护 `main` post-merge run 成功。

## 实施记录

- Data Center commit `d19695c17e4fb16818734d34e5775ff5c59704e7` 实现 manifest-derived
  schema provenance、cached immutable catalog snapshot、HMAC opaque cursor、stable keyset pagination、
  API additive metadata、query metrics/logging、Python client 与 Web UI 显式分页。
- `macro-market-lab` commit `25707938d4894fb58c8f5ea47da0c26a12bae0f2` 将
  `DataCenterReadClient` 迁移为显式分页并增加 opaque cursor 回归测试。
- 统一本地 CI 于 2026-09-10 通过：74 tests、backend/scripts lint、dependency/contract check、
  secret scan、operations acceptance、Web UI build 与隔离 API/worker service acceptance 全部成功。
- 百万行/千 part 基准 receipt：
  `/home/quant/market_lake/evidence/data-center/query-contract-20260910/query-benchmark-d19695c.json`。
  数据规模 1,000 parts / 1,000,000 rows，1,000-row query cold 1.1094s、warm P95 0.4392s、
  additional peak RSS 257.26 MiB，全部低于预算。
- 生产分页与 consumer parity receipt：
  `/home/quant/market_lake/evidence/data-center/query-contract-20260910/query-pagination-d19695c-2570793.json`。
  SPY bars、PAYEMS current/PIT 的 paged/unpaged row count、min/max、稳定 hash 和 snapshot 均一致；
  `macro-market-lab` bars/economic consumer parity 通过。
- 本机 systemd API 已加载实现并通过 readiness；生产 PAYEMS mixed fixture 返回
  `schema_versions=[economic_observations.v1,economic_observations.v2]` 和兼容字段 `mixed`。
- `macro-market-lab` PR #2 已合并为 protected `main` commit
  `0bc33e041fba0863667a2dbced555d0530dbcf5d`。
- Data Center PR #2 的 required `verify` 成功后合并为 protected `main` commit
  `b342e64614ccf3bf1a97c4afef4c51142af17515`；post-merge GitHub Actions run
  `34447619968` / job `102775793301` 的 `verify` 于 2026-09-10 成功。

截至 2026-09-10，Q1–Q4 的实现、自动化测试、真实 mixed fixture、catalog/pagination 稳定性、
consumer parity、百万行/千 part 性能预算、本机生产服务和受保护 `main` post-merge 门禁均已完成。

## 非目标

- 不在本阶段引入 Postgres、Redis、Elasticsearch 或分布式 query engine；
- 不修改 canonical Parquet 数据或重写历史 manifest；
- 不通过字段猜测、fallback zero 或静默跳过损坏 part 来维持可用性；
- 不在 consumer 完成分页迁移前移除现有完整结果读取行为；
- 不把 Web UI 重写为新的前端框架。
