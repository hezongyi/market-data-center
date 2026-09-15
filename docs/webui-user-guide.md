# Market Data Center WebUI 使用手册

适用版本：**v0.4.1**（生产 deployment `49ddbc55361d-bd11ad0e`，tag `v0.4.1`，source commit `49ddbc5`）
编写依据：`webui/src` 源码、`docs/api-and-webui-contract.md`，以及 2026-09-13 对生产实例的实测调用。
本文只描述界面上真实存在的按钮、字段和状态；凡未在代码中出现的功能，本文不会声称存在。

> 开发预览补充（P2，尚未生产发布）：新主线入口的“数据任务”页支持从
> capabilities 创建 EURUSD/BID/1m→5m 计划、固定窗口预览、启用并立即运行、
> execution/step 跟踪以及 raw/derived 同页读回。以下 §1.4 只适用于明确标识为
> fixture 或 live 的隔离预览，不改变本文记录的生产版本事实。

---

## 0. 先回答三个最常见的问题

1. **界面在哪？** API 与 WebUI 由同一个进程提供，生产监听 `127.0.0.1:18380`，浏览器打开 <http://127.0.0.1:18380/> 即是控制台首页。
2. **要不要密码？** 看数据不需要；**改数据（排队任务、重试失败 run、确认死信、流转 finding 状态）需要 API key**，在右上角 `Session access` 里临时填一次。
3. **会不会误操作？** 控制台把"预览"和"提交"拆成两步：`Validate and preview` 无副作用，只有 `Confirm and queue` 才真的排队；而且它自己不算执行结果，最终状态以 worker 发布的 run receipt 为准。

---

## 1. 打开界面与授权

### 1.1 访问地址

生产 API 绑定在 loopback，WebUI 是同源托管的静态构建（`DATACENTER_WEBUI_DIST`）。两种打开方式：

| 场景 | 做法 |
|---|---|
| 你就在这台服务器上（有桌面/浏览器） | 直接打开 <http://127.0.0.1:18380/> |
| 你从自己电脑远程访问 | 先建隧道，再打开本地地址：<br>`ssh -L 18380:127.0.0.1:18380 quant@192.168.7.232`<br>然后浏览器访问 <http://localhost:18380/> |

注意：

- 不要用 `file://` 直接打开 `webui/dist/index.html`。前端所有请求都发往同源 `/api/v1`，脱离服务进程会全部失败。
- 页面**没有**登录页、没有账号体系。它是一个运维控制台，安全边界是"谁能连到这台机器 + 谁持有 API key"。

### 1.2 API key（Session access）

- 位置：右上角 **`Session access`** 按钮 → 展开面板 → `API key` 输入框（密码框）→ `Done` 收起。
- 生效范围：**只保存在浏览器内存里**，随每次请求以 `X-API-Key` 头发送；刷新页面即丢失，不会被写进 localStorage 或磁盘。
- key 存在服务器上的 `~/.config/market-data-center/env` 的 `DATACENTER_API_KEY`（**不要把它粘进聊天、工单或截图**）。
- 哪些操作需要它（生产已配置 key，因此下列写操作没有 key 必定 401）：

| 需要 key（写 / 改状态） | 不需要 key（只读） |
|---|---|
| `Confirm and queue`（维护任务）、`Queue ingest`（Operations 页） | 所有列表、详情、覆盖率与运维面板查询 |
| Runs 页的 `Retry`、`Acknowledge` | `Validate and preview`（`POST /maintenance/plans`，无副作用，刻意不鉴权） |
| Quality 页的 finding 状态变更（acknowledge/resolve） | Operations 页各只读面板 |

- 没填 key 时的表现是可识别、可恢复的：页面弹出 **`Not authorized`** / **`Write requests need a valid API key. Open Session access and try again.`**，数据本身照常显示。

### 1.3 启动后第一眼该看什么

进入首页先确认三处，30 秒内就能判断"平台是否健康、我连的是不是生产"：

1. 左侧栏底部：`API ready`（不是 unavailable）、`Snapshot fresh`、以及 **版本号 · deployment id**（应为 `0.4.1 · 49ddbc55361d-bd11ad0e`）。
2. 顶部右侧容量徽章：`ok` / `warning` / `critical`。
3. 首页 `Capacity …` 横幅与四个指标卡是否出现异常红色。

### 1.4 P2 隔离预览：EURUSD 手动闭环

1. 打开预览卡给出的 UI URL，首次使用以 `admin` 设置至少 12 位密码并登录。
2. 进入“数据任务”→“创建任务”。数据源、品种、原始周期和价格基准全部来自
   `GET /api/v1/capabilities`；P2 预览只开放 Dukascopy/EURUSD/1m/BID。
3. 保持“固定窗口”，选择最近一个完整交易日，勾选“派生 5m 数据”，先“校验
   任务”，确认无冲突和字段错误后保存。
4. 在详情页点击“启用并立即运行”。页面每 2.5 秒刷新 execution、raw/derive
   steps 和数据样本；只有 execution 终态及数据读回才代表闭环完成。
5. raw 和 5m 卡片各有“复制 HTTP 请求”。同源只读例子如下（URL、任务和时间
   范围以实际预览为准）：

```bash
curl -sS 'http://127.0.0.1:20070/api/v1/capabilities'
curl -sS 'http://127.0.0.1:20070/api/v1/production/tasks/<task-id>'
curl -sS 'http://127.0.0.1:20070/api/v1/production/tasks/<task-id>/executions?page_size=10'
curl -sS 'http://127.0.0.1:20070/api/v1/production/executions/<execution-id>/steps'
curl -sS 'http://127.0.0.1:20070/api/v1/runs/<run-id>/detail'
curl -sS 'http://127.0.0.1:20070/api/v1/runs/<run-id>/manifest'
curl -sS 'http://127.0.0.1:20070/api/v1/bars?provider=dukascopy&symbol=EURUSD&timeframe=1m&page_size=100'
curl -sS 'http://127.0.0.1:20070/api/v1/provider-bars/coverage?provider=dukascopy&symbol=EURUSD&timeframe=1m&start=<UTC>&end=<UTC>'
curl -sS 'http://127.0.0.1:20070/api/v1/market-bars?provider=dukascopy&symbol=EURUSD&timeframe=5m&price_basis=bid&recipe_id=utc-24x7-1m-to-5m-ohlcv&recipe_version=1&page_size=100'
curl -sS 'http://127.0.0.1:20070/api/v1/market-bars/coverage?provider=dukascopy&symbol=EURUSD&timeframe=5m&price_basis=bid&recipe_id=utc-24x7-1m-to-5m-ohlcv&recipe_version=1&start=<UTC>&end=<UTC>'
```

fixture 模式保留 Dukascopy 业务身份，但 receipt 的 connector version 为
`isolated-preview-fixture-v1`，不访问真实源。live 模式必须使用独立 preview id，
固定 EURUSD 与不超过 24 小时的 UTC 窗口，并设置请求数和磁盘预算；fixture 与
live 数据根不能混用。

---

## 2. 界面骨架

### 2.1 左侧导航（8 个工作区）

| 导航项 | 中文含义 | 一句话用途 | 是否写数据 |
|---|---|---|---|
| **Overview** | 总览 | 一屏看容量、24h 运行量、失败率、未处理 finding | 否 |
| **Data catalog** | 数据目录 | 看 3 个数据集的健康状况与物理覆盖 | 否 |
| **Maintenance** | 维护工作台 | **维护任务的唯一入口**：预览 → 排队各类任务 | **是** |
| **Runs** | 运行记录 | 查每次运行的阶段、结果、窗口、重试链；重试与确认死信 | **是**（retry/acknowledge） |
| **Quality** | 质量 | 看 findings、流转处理状态、按 finding 生成修复任务（校验 run 要去 Maintenance 发起） | **是**（finding 状态、修复任务） |
| **Explorer** | 数据浏览 | 直接查行情/宏观序列与覆盖率，可把查询条件交给 Maintenance | 否 |
| **Operations** | 运维 | 队列、worker 心跳、容量历史、receipts、写审计 | **是**（Queue ingest） |
| **Production plans** | 生产计划 | **统一调度与生产任务的唯一入口**：计划注册表、向导与编辑、调度器状态与退避、目录矩阵与治理单元 | **是**（计划生命周期） |

侧栏左上角的 `‹ / ›` 按钮可折叠侧栏（窄屏时给表格让位）；底部 `Refresh data` 重新拉取全局数据（不做写操作）。

### 2.2 顶部与全局状态语义

- 标题：当前工作区名；Overview 页标题固定为 `Good morning, data center`。
- 右侧：容量徽章 `ok|warning|critical`，以及 `Session access`。
- 顶部通知条（notice）：每次提交/操作后的短消息，例如 `Queued ingest · 1 run(s) · run 1a2b3c4d`。
- 页面级空状态：`∅` 图标 + 一句说明（例如 `No runs recorded`）；错误状态是 `Unable to load data` + 具体 message + `Try again` 按钮；未授权是锁定态（多数页面写 `Not authorized`，Quality / Data catalog 写 `A valid API key is required`）。三者刻意区分：**"加载失败"和"权限不足"和"确实没有数据"绝不混为一谈。**

### 2.3 四种全局语义（贯穿所有页面）

| 维度 | 取值 | 含义 |
|---|---|---|
| 查询状态 | `loading` / `success` / `empty` / `error` | `empty` 是"查询成功但结果为空"，不是失败 |
| 权限 | `authorized` / `unauthorized`(401) / `protected`(507) | 507 表示容量保护拦下了写操作，**读仍然可用** |
| 新鲜度 | `fresh` / `stale` / `unknown` | 依据 readiness 的 `operational_snapshot_status`；`unknown` 表示"不知道"，不会假装健康 |
| 写状态机 | `draft` → `validating` → `confirming` → `queued` → `running` → `pass` / `degraded` / `failed` | **`queued` 不是成功**，只表示已被接受 |

---

## 3. 核心概念速查（看界面之前先懂这几个词）

### 3.1 三个数据集（dataset）

| dataset_id | 类型 | 内容 | 说明 |
|---|---|---|---|
| `provider_bars` | raw | 标准化 provider 原始行情 bars | 上游抓取层，按 provider/symbol/timeframe 分区 |
| `market_bars` | derived | 按版本化 recipe 生成的 canonical market bars | 由 `derive` 从 `provider_bars` 或自身派生 |
| `economic_observations` | raw | 标准化宏观时间序列（FRED） | 用 `series_id` 而非 symbol；支持 `current` / `pit` 查询模式 |

### 3.2 六种 run kind（维护任务的类型）

Maintenance 页把每种任务画成一张卡片，卡片上写明它写哪个数据集。生产 `/capabilities` 实测矩阵如下：

| run kind | 卡片文案 | 目标 dataset | 会产生 canonical part 吗 |
|---|---|---|---|
| `ingest` | Provider ingest — Fetch raw bars from a registered provider | `provider_bars`、`economic_observations` | 是 |
| `backfill` | Backfill — Shard a wide historical range into bounded windows | `provider_bars`、`economic_observations` | 是 |
| `gap_repair` | Gap repair — Re-fetch only the intervals coverage reports as missing | 仅 `provider_bars` | 是 |
| `derive` | Derive — Run a registered transform recipe over an input snapshot | 仅 `market_bars` | 是 |
| `quality` | Quality check — Verify a selector and record findings; publishes nothing | `provider_bars`、`economic_observations` | **否（只读校验）** |
| `parity` | Parity check — Verify derived rows against the raw layer that produced them | 仅 `market_bars` | **否（只读校验）** |

界面对**不可用的组合直接禁用**（灰掉并给出 title 提示 `… is not available for …`），而不是让你提交后被后端 422 打回。矩阵来源是后端 `GET /api/v1/capabilities`，前后端同源，不各自维护一份。

### 3.3 四种 run scope

`production`（正式数据）、`maintenance`（例行维护）、`migration`（迁移）、`acceptance`（验收/演练）。scope 只用于**分类与统计**，不同 scope 的 run 在同一个队列里排队；用错的代价是后续按 scope 统计时口径混乱，建议：动生产数据一律 `production`，演练用 `acceptance`。

### 3.4 run 的生命周期与字段

- **status**：`queued` → `running` → 终态 `pass` / `failed` / `dead_letter`。
- **outcome**（工作台投影，比 status 更细）：`pass` / `degraded`（跑完了但有降级原因）/ `failed` 等。
- **stage**：运行推进到的阶段（例如抓取、校验、发布）。
- **manifest_status**：`published`（发布了 canonical manifest）/ `not_applicable`（**只读校验类 run 不产出 manifest**）/ 其它未发布原因。
- **windows**：大范围任务被切成多个**半开区间** `[start, end)` 窗口逐个执行；窗口数是预览里最该看的数字。
- **degraded_reasons**：不致命但必须知道的降级原因（列表里显示为带数字的三角徽章，鼠标悬停看全文）。
- **findings**：这次运行留下的质量问题记录数（不是失败，是需要人看）。
- **retry chain**：重试谱系（原有终态 run 不会被改写，重试是**新 run**）。

### 3.5 finding 的处理状态机

`open` →（`acknowledged`，我看到了，先接受）→ `resolved`（已解决，可关联 `resolved_by_run_id`）。
`finding_id` 是稳定身份：同一个问题重复出现不会新建 id，只累加 `occurrence_count` 并刷新 `last_observed_at`。**处理状态与运行结果严格分离**——把 finding 标成 resolved 不会篡改任何历史 run。

### 3.6 两类典型拒绝（看到别慌）

| 界面提示 | HTTP | 含义 | 你该做什么 |
|---|---|---|---|
| `Not authorized` | 401 | 没填/填错 API key | `Session access` 填 key 重试 |
| `Write protected` / `Capacity protection blocks this task.` | 507 | 容量保护（critical，或 warning 下超过 31 天的 backfill） | 去 Operations 看容量，先清理/扩展磁盘；**读取不受影响** |
| `Blocked by validation` | 422 | 参数/矩阵不合法（带稳定 `code`） | 按预览里的红色错误逐条改 |

---

## 4. Overview（总览）

**用途**：值班第一屏。回答"平台现在健康吗、有没有需要我处理的运行、有没有欠着的质量债"。

### 4.1 页面结构（自上而下）

1. **容量横幅**：`Capacity ok|warning|critical` + `<free_ratio>% free. Warning threshold <n>%.`（阈值来自后端配置，生产当前为 warning **5%** / critical **2%**，不是源码默认的 15%/10%）；右侧两个快捷按钮：
   - `Create maintenance task →` 跳到 Maintenance；
   - `Review capacity →` 跳到 Operations。
   - 容量 `critical` 时横幅变红。
2. **四个指标卡**：

   | 卡片 | 数值含义 | 备注行 |
   |---|---|---|
   | `Datasets` | 已发布的数据集数量（生产为 3） | `published schemas` |
   | `Runs (24h)` | 24 小时内运行数 | `queue depth N`（当前排队深度） |
   | `Failure rate (24h)` | 24h 生产终态运行失败率 | `production terminal runs`；>0 时卡片转黄 |
   | `Open findings` | **仅统计 `open`** 的 findings | 有值时 `review required` |

3. **Recent runs**（eyebrow `Live activity`）：最近 5 条运行（Run ID / Dataset / Status / Rows），右上 `View all →` 去 Runs 页。
4. **Degraded and failed runs**（eyebrow `Needs attention`）：最多 5 条 `degraded`/`failed`/`dead_letter` 运行；没有时显示 `Nothing needs attention`。右上 `Inspect in Runs →`。
5. **Service health**（eyebrow `System signal`）：`Ready` 大标题 + `operational snapshot fresh`，以及 Read path / Write path / Worker heartbeat / Deployment 四行。

### 4.2 怎么用

- **值班 30 秒巡检**：看横幅颜色 → 看 `Failure rate (24h)` → 看 `Open findings` → 有需要处理的点进 Runs。
- `Open findings` 只算未处理项，所以它归零代表"质量债已认领或已解决"，不代表"从未出现过问题"。

---

## 5. Data catalog（数据目录）

**用途**：数据资产的**只读**注册表 —— 看平台发布了哪些数据集、raw→derived 的血缘关系、各 provider 的能力边界，并对单个数据集做一次覆盖度探测。
**本页所有请求都是 GET**：`/capabilities`、`/provider-bars/coverage`、`/economic/coverage`；页面上任何按钮都不会写后端数据。

### 5.1 自上而下三个面板

**面板一：`Registry` / `Data asset catalog`**（右上徽章 `Registry published` / `Writes protected` / `Read model unavailable`）

- `Search datasets` 输入框（占位 `Dataset, schema, kind, description`）：纯前端过滤，只匹配**数据集名 / 描述 / schema 版本 / kind** 四项。
- 计数提示：`N dataset(s) · raw and derived layers are labelled separately`。
- 数据集**表格**（不是卡片），列：

  | 列 | 内容 |
  |---|---|
  | `Dataset` | 数据集名（粗体） |
  | `Kind` | 分层标签：`raw · provider feed`（provider 原始层）、`raw · economic series`（宏观序列）、`derived · recipe output`（配方产出）；未发布时 `Kind not published` |
  | `Schema` | schema 版本徽章 |
  | `Partitioning` | 分区键，`/` 分隔 |
  | `Quality profile` | 质量配置名 |
  | `Query modes` | 查询模式芯片（如 `current`、`pit`） |
  | `Materialization` | 物化策略（如 `persisted`） |
  | `Producing recipes` | 产出该数据集的配方 `recipe_id@version`；raw 层显示 `Raw layer · persisted by ingest, not by a recipe.` |
  | `Actions` | `Open Explorer`；有可写 run kind 时多一个 `Create maintenance task` |

- 点击任意行 → 打开右侧详情抽屉。

**面板二：`Lineage` / `Raw → derived recipes`**（徽章 `N registered` / `None published`）
每行显示 `input · provider_bars` → `output · market_bars`、`recipe_id@version`，以及 `1m → 1d · <session_profile> session · <materialization>`、`price basis · …`、`partial buckets · …`、`missing input · …`。生产当前注册 8 个 recipe（`utc-24x7-1m-to-{5m,15m,30m,1h,4h,1d}-ohlcv`、`utc-24x7-1d-to-{1w,1mo}-ohlcv`）。

**面板三：`Capability read model` / `Provider capability`**（徽章 `N provider(s)`）
每个 provider 一张卡片：

- 头部：provider 名、`N approved instrument(s)`（或 `No instruments`）、`max window N day(s)`（单窗口最大天数，超过就要分批）。
- 事实区：`Asset classes` / `Timeframes` / `Maintenance timeframes` / `Price bases` / `Max window days` / `Session profile`；**缺失的能力显示 `Not published by the API`，绝不猜测**。
- 标的表（有已审批标的时）：`Symbol` / `Asset class` / `Currency` / `Session profile` / `Approval`（`approved` / `not approved`）。
- 生产实测：`binance`（1m/1h/1d）、`dukascopy`（1m…1d，维护仅 1m，price basis `bid`）、`fixture`（测试用）、`yfinance`（1d，`adjusted`/`raw`）。

### 5.2 数据集详情抽屉

标题 = 数据集名。字段：`Kind`、`Schema version`、`Description`、`Partitioning`、`Quality profile`、`Query modes`、`Materialization`、**`Writable by`**（哪些 run kind 能写它）。

往下三块：

1. **`Raw → derived relation`**：derived 数据集列出产出它的 recipe；raw 数据集说明 `Raw layer: this dataset is persisted by ingest, not produced by a recipe.`
2. **`Published coverage`（覆盖度探测）**：填表 → `Load coverage` → 结果。

   | 字段 | 默认 | 说明 |
   |---|---|---|
   | `Provider` | `fixture`（economic 数据集为 `fred`） | 自由文本 |
   | `Symbol` / `Timeframe` | `UI_TEST` / `1d` | 非 economic 数据集 |
   | `Series ID` | `PAYEMS` | 仅 `economic_observations` |
   | `Coverage start date` / `Coverage end date` | 空 | **两个都填**才会返回 ready intervals 与 gap 统计（只填一个时页面会提示 `Set both coverage dates …`） |

   结果字段（行情类）：`Coverage scope`、`Readiness`、`Rows`、`Gap count`、`Ready intervals`、`First / last bar`、`Ready interval list`。
   结果字段（宏观类）：`Rows`、`First observation`、`Last observation`。
   - 若后端只给摘要，`Readiness` 会显示 **`unknown (summary only)`** 并附说明：`Summary-only coverage: the API returned coverage_scope=summary with readiness unknown, so no per-interval readiness is claimed here.` —— 这类情况不会假装"ready"。
   - 生产实测示例（`provider_bars` + `dukascopy` + `EURUSD` + `1m`）：`row_count=2329`、`gap_count=2760`、`physical_coverage=present`、`session_coverage=incomplete` —— 看到这种组合就是 gap repair 的典型场景。
3. 底部两个按钮：`Open data explorer`（跳 Explorer）、`Create maintenance task`（跳 Maintenance）。

### 5.3 与其他工作区的衔接（有一个反直觉的地方）

| 动作 | 实际行为 |
|---|---|
| `Open Explorer` / `Open data explorer` | 切到 Explorer，并**只按数据集选模式**（`economic_observations` → 宏观模式，其余 → 行情模式），**不预填** provider/symbol/series；想看 `market_bars` 还要在 Explorer 里手动切到 `Market bars` |
| `Create maintenance task` | 切到 Maintenance 工作区，**不会把数据集预填进表单**（本页调用跳转时不带参数）。预填只有 **Explorer** 才做得到 |

也就是说：从目录进 Maintenance 后，请自己重新选 provider/symbol/timeframe，然后照 §6.4 的流程预览再提交。

### 5.4 状态与异常

- 表格为空：`No matching datasets.`（搜索无匹配时）。
- capabilities 拉取失败：右上徽章变 `Read model unavailable`；若同时是 401，表格下方显示 `Not authorized`，并说明 `Dataset rows fall back to the legacy registry, which cannot describe kind, quality or lineage.`（回退到老接口时，Kind/质量/血缘会缺失）。
- 面板二/三失败时分别提示 `Recipe registry unavailable.` / `Provider capabilities unavailable.`，且明确写着 `Nothing is inferred locally.`（本地不做任何推断）。
- 探针错误：`Unable to load coverage` + `Try again`。

---



## 6. Maintenance（维护工作台）

**用途**：所有会动数据的维护任务都从这里发起（抓取、补数、派生、校验）。设计上强制两步：**先预览（无副作用）→ 再确认排队**。

> 说明：写操作并非只在本页发生 —— Runs 页的 `Retry`/`Acknowledge`、Quality 页的 finding 状态与修复任务、Operations 页的 `Queue ingest` 同样是写操作。但"创建维护任务"这条路径只有这里（`POST /maintenance/tasks`）。

### 6.1 页面结构

1. **任务表单**（`Data maintenance` / `Create a maintenance task`），右上角写状态徽章：`Writes available` / `Writes protected` / `Capabilities unavailable`。
2. **模板条**：`Task template`（下拉，`— none —`）、`Template name`（输入，占位 `nightly dukascopy 1m`）、`Save template`、`Delete template`。
   - 模板**只存参数、绝不存凭据**，存在浏览器 localStorage（最多 20 个），换浏览器就没了；每次套用后**仍需重新预览**。
3. **Run kind 卡片区**（6 张，单选）：点击切换任务类型，卡片上写着目标 dataset，不可用的组合直接禁用。
4. **参数区**：

   | 控件 | 默认值 | 说明 |
   |---|---|---|
   | `Run scope` | `Production` | Production / Maintenance / Migration / Acceptance |
   | `Provider` | `fixture` | 含 `fred (economic series)` 选项；**`fixture` 是测试用 provider，真实抓取请选 `dukascopy` / `binance` / `yfinance`** |
   | `Symbol` | `UI_TEST` | **这是示例值，务必改成真实标的**（如 `EURUSD`） |
   | `Asset class` | 空（占位 `auto`） | 留空即自动推断 |
   | `Timeframe` | `1d` | 选项来自所选 provider 的能力（维护任务用 `maintenance_timeframes`，例如 dukascopy 只允许 `1m`） |
   | `Start (UTC)` / `End (UTC)` | `2026-01-01` / `2026-01-05` | 日期选择器，按 UTC 解析为**半开区间** `[start, end)` |
   | 选 `fred` 时 | `Series ID` 替代 Symbol（默认 `PAYEMS`），dataset 自动切到 `economic_observations` | |

5. **recipe 参数区**：仅当目标 dataset 是 `market_bars`（`derive` / `parity`）出现 —— `Recipe`（生产已注册 8 个，如 `utc-24x7-1m-to-1d-ohlcv`）、`Recipe version`（默认 `1`）、`Price basis`。旁边提示会显示该 recipe 的 `provider_bars 1m → 1d · … partial buckets` 语义；recipe 未注册时会提示 `Recipe is not registered; the preview will report it.`
6. **表单按钮**：`Validate and preview`（主）、`Clear`。
   - 没 key：出现 `Write requests need a valid API key. Open Session access and try again.`
   - 容量保护：出现 `Capacity protection blocks this task.`

### 6.2 预览面板（`Validation preview` / `Review before submitting`）

徽章三态：`Ready to submit` / `Blocked by validation` / `Blocked by capacity`。四张卡片：

| 卡片 | 看什么 |
|---|---|
| 窗口卡 | `N half-open window(s)` + 规划原因（如 `gap_repair`）与 session profile（如 `dukascopy_fx_weekdays_utc`） |
| 数据集卡 | `dataset_id` / `provider · timeframe` / `plan_id` 前 16 位 |
| 容量卡 | `ok` / `warning` / `critical` / `capacity · 11.7% free` / `ingest policy · 2 day(s) requested`；受保护时卡片高亮 |
| 快照卡 | `input part(s)` 数量 + `input_snapshot_id`（或 `no snapshot required`） |

下面按需追加：

- **覆盖率明细**（有 `coverage` 时）：`Coverage readiness`（`ready` / 其它）、`Gap count`、`Ready intervals`、`Latest complete boundary`。
- **标题为 `Validation errors` 的红色列表**：每条含字段名、人话说明和稳定 `code`。
- **`Capacity protection` 黄色条目**：`protected_reason` 的说明与 code。
- **`Task warnings` 蓝色 `notice`**：不阻断提交，但务必读。

实测样例（生产、`gap_repair` dukascopy EURUSD 1m、2026-09-01→09-03）：`window_count=46`，`validation.status=valid`，`capacity.ok`（`free_ratio≈11.7%`，`requested_days=2`），覆盖率显示 `gap_count=2760`、`session_coverage=incomplete` 但 `physical_coverage=present` —— 这正是该用 gap repair 而不是整段 backfill 的典型场景。

### 6.3 提交与跟踪

- 点 **`Confirm and queue`** 才真正排队；按钮下方写明：`Queued work returns a run id; the receipt is only final once the worker publishes it.`
- 成功后出现 **`Submitted tasks`** 面板（`Live activity`）：表格列出本会话提交的任务（`Task` / `Kind` / `Dataset` / `Windows` / `Submitted (UTC)`，最多保留 6 条），下面是被跟踪 run 的实时列表（run id、outcome、stage、window 数、降级原因）。
- 跟踪徽章会经历 `Queued → Running → Passed / Degraded / Failed`；跟踪器最多轮询 90 次 × 1.5s，超时不代表失败。
- `Edit parameters` 可回到编辑态；`Clear` 复位表单与预览。

### 6.4 推荐操作流程（照着做即可）

1. 选 run kind 卡片 → 2. 选 provider / symbol / timeframe / 时间范围 →（`derive`/`parity` 再选 recipe）→ 3. `Validate and preview` → 4. 读四张卡 + warnings，确认 `window_count` 符合预期 → 5. `Confirm and queue` → 6. 在 `Submitted tasks` 里盯到终态 → 7. 去 Runs 页看详情（窗口、重试链、manifest）。

> 经验：窗口数远大于预期，通常说明时间范围太宽（该用 backfill 并合理分批）或缺口太多（该用 gap repair 而不是全量重灌）。

---

## 7. Runs（运行记录）

**用途**：查"每一次运行到底发生了什么"，并对失败/死信做处置。

### 7.1 筛选与列表

筛选条：`Status`（all/queued/running/pass/failed/dead_letter）、`Run kind filter`、`Run scope filter`、`Dataset filter`（占位 `provider_bars`）、`Runs created from` / `Runs created through`（日期）。

表格列：`Run ID`（等宽字体）、`Dataset`、`Kind`、`Scope`、`Outcome`（徽章 + 降级原因数量）、`Stage`、`Windows`、`Manifest`（`published` 带图标，其它显示为去下划线的文字）、`Findings`、`Created (UTC)`、`Actions`。

- **排序**：任意列头可点击切换升/降序。
- **翻页**：底部 `Previous` / `Next page` + `Page N · M run(s)`；每页 25 条。
- **游标语义**：`Cursor paging is bound to these filters; changing a filter restarts at page 1.` — 改筛选条件会回到第 1 页（避免跨筛选复用游标导致的错页）。
- **行内操作**：仅当 `status` 是 `failed` / `dead_letter` 时出现 `Retry`；死信且未确认时出现 `Acknowledge`。点击行其它位置打开右侧详情抽屉。

### 7.2 详情抽屉（点击任一行的 Run ID）

顶部：outcome 徽章、`stage …`、`terminal`（终态标记）。随后：

- **降级原因列表**（若有）：来源、说明、code。
- **属性表**：`Dataset`、`Run kind`、`Run scope`、`Selector`（`provider=… symbol=…`）、`Time range`（`start → end (half-open)`）、`Windows`、`Input snapshot`、`Schema version`、`Rows`、`Extent (UTC)`、`Manifest`、`Output hash`（前 16 位）、`Findings`、`Attempts`（`N · retries M`）、`Created (UTC)`、`Finished (UTC)`；失败时还有 `Error`，死信时有 `Dead letter` 状态。
- **Windows**：每个窗口的序号、原因、`start → end`、行数。
- **Retry chain**：重试谱系（run id、状态、关系、阶段、时间）。
- **Attempt errors**：每次尝试的失败阶段、错误类型、错误信息。
- **Findings**：该 run 记录的 finding（severity、说明、`finding_id`）。
- **Manifest**：`published` 时才有 `Load manifest` 按钮，点开显示完整 JSON；否则提示 `Verification runs record findings and publish no canonical manifest.`（校验类）或 `No published manifest is available for this run.`
- 底部固定提示：`Terminal receipts cannot be rewritten by this console.` —— 控制台**不会**改写历史 receipt。

### 7.3 Retry 与 Acknowledge 的区别

| 操作 | 二次确认文案 | 实际效果 |
|---|---|---|
| `Retry` | `Retry failed run?` — `A new run will be queued. The original terminal run remains immutable.` | **新建**一个 run，原 run 保持终态不变 |
| `Acknowledge` | `Acknowledge dead letter?` — `This records an additive acknowledgement and does not modify the terminal run.` | 只追加一条确认记录，不动原 run |

两者都会记录到写审计（Operations 页可查），且都需要 API key。

---

## 8. Quality（质量反馈闭环）

**用途**：把"观测到的数据缺陷"集中起来处理。核心立场是**finding ≠ 作业失败**：一个 finding 记录的是"数据有问题"，它带着上报它的 run、数据位置和处理状态三份独立事实。页面只在事实足够界定时才提供"创建修复任务"。

> **注意**：Quality 页**不能发起质量校验 run**。要跑校验请去 Maintenance 选 `Quality check` / `Parity check`，跑完回本页刷新即可看到新 findings。

### 8.1 列表页

面板标题 `Quality feedback` / `Quality findings`，头部有：

- 处理状态计数（`0 open · 0 acknowledged · 0 resolved`，全库口径）；
- `N on this page`（**本页条数，不是总数**）；
- `Refresh →`（按当前筛选与游标重查）。

筛选条（**服务端筛选**）：`Severity`、`Finding code`、`Quality dataset`、`Finding state`（all/open/acknowledged/resolved）、`Finding run`（占位 `run id`，只在本页支持）、`Finding from date`、`Finding through date`。
说明文字：`Filter options come from the findings this console has loaded; the API exposes no distinct-value endpoint.` —— 下拉候选来自"已加载过的 findings"，不是后端全集，这是已知限制。

表格列：

| 列 | 含义 |
|---|---|
| `Severity` | 严重度徽章：`error`/`high`/`critical` 红、`warning`/`medium` 黄、其它灰 |
| `Code` | finding 代码，如 `coverage_not_ready`、`coverage_degraded`、`lineage_drift`、`parity_missing_outputs`、`source_degraded` |
| `Dataset` | 所属数据集 |
| `Observed (UTC)` | 观测时间（优先 bar 时间戳/观测日期，否则首末观测时间） |
| `Run` | **首次上报**该缺陷的 run（缺失显示 `not recorded`） |
| `State` | 处理状态徽章（`open` 黄 / `acknowledged` 灰 / `resolved` 绿） |
| `Occurrences` | 同一缺陷被重复观测的次数（悬停显示首次/最近时间） |

分页：`Previous` / `Page N · M finding(s)` / `Next`，每页 25 条；**游标绑定筛选集**，改筛选回到第 1 页。若过期游标报错，用错误态的 `Restart paging` 从第 1 页重来。

页脚 `Run outcomes in this workspace` 是**词表**（只读），列出 4 个 outcome 的含义，用来区分"数据集降级"和"作业失败"：`failed`（作业停了）/ `degraded`（跑完但降级）/ `coverage_not_ready`（数据集缺时间戳，不是作业失败）/ `provider_gap`（provider 没返回该区间）。

### 8.2 finding 详情抽屉（点任意行）

- 顶部：severity 徽章、state 徽章、code。
- 若 code 是 `coverage_not_ready` / `coverage_degraded`，会额外标注这是 **degraded dataset**，不是作业失败。
- 事实表：`Finding id`、`Severity`、`Code`、`Dataset`、`Message`、`Selector`、`Bar timestamp (UTC)`、`Observation date (UTC)`、`Occurrences`、`First observed (UTC)`、`Last observed (UTC)`、`Handling state`（resolved 时还有 `Resolved by run`）。
- **`Recorded coverage · <layer>`**（有覆盖度报告时）：`Readiness`、`Gap count`、`Missing timestamps`、`Expected timestamps`、`Rows recorded`、`Latest complete boundary`，以及 `Ready intervals (N)` 列表（最多显示 8 条，其余折叠为 `… N more recorded interval(s)`）。
- **`Run linkage`**：`Reporting run`（首次上报）与 `Last observing run`（最近一次观测），各有 `Open run` / `Open last observing run` 按钮，点开直接看该 run 的 outcome、stage、`Manifest status`、窗口、findings 与降级原因。
- **`Handling state`**：三个按钮 `Acknowledge finding` / `Mark resolved` / `Reopen finding`，外加 `additive` 说明 —— **状态变更只表示"人已处理"，不会修复数据，也绝不改写上报它的 run 回执**。
- **`Create repair task`**：见下。

### 8.3 从 finding 生成修复任务（本页唯一"能修东西"的路径）

页面按 `code + dataset` 判定可修复性：

| 条件 | 生成的草稿 |
|---|---|
| `provider_bars` + `coverage_not_ready` / `coverage_degraded` | **`gap_repair`**（按记录的缺口区间补数） |
| `market_bars` + 五种可修 code 之一 | **`parity`**（只读对账校验，不产出数据） |
| 其它组合 | `blocked`：`No bounded repair task is defined for code … on ….`，只能点 `Open maintenance workspace` 手动规划 |

`available` 时先显示事实摘要（`Run kind` / `Run scope` / `Dataset` / `Window (UTC, half-open) start → end`）与来源说明（例如 `the first gap the coverage report records, closed by the next ready interval`），然后：

1. `Create repair task` → 调预览（`POST /maintenance/plans`，无副作用）；
2. 在 `Review before submitting` 面板核对四张卡（窗口数 / dataset+provider+timeframe+plan id / capacity / run scope+快照）与校验错误、容量保护、warning；
3. 徽章为 `Ready to submit` 时点 `Confirm and queue`；
4. 排队成功显示 `Task … queued N window(s) as …`，此时可选：
   - `Link run to finding` → 确认框 `Record this run as resolving the finding?` → `Mark resolved by run`（把 run 记为解决方案）；
   - `Not now` → 保持当前处理状态（`The finding stays in its current handling state until a run is linked.`）。

> 提醒：关联 run **只是记录**。run 之后失败不会自动把 finding 退回 `open`；请自己看 run outcome 决定是否 `Reopen finding`。
> 另外 `Open maintenance workspace` **不携带**已评估好的参数，需要手动重选（Explorer 才会带草稿）。

### 8.4 状态与异常文案

- 空表：`No quality findings.`
- 未授权（401/403）：`A valid API key is required` + `Open Session access with a valid API key, then reload this workspace.`
- 写入被容量保护（507）：显示 `Write protected`（不是"加载失败"）。
- 上报 run 详情加载失败：`The reporting run detail could not be loaded: …`

---

## 9. Explorer（数据浏览）

**用途**：只读浏览已发布不可变快照中的数据（原始行情 / 派生行情 / 宏观序列），同时看**该 selector 的覆盖率、ready intervals 与缺口**，并把 selector+窗口"交接"给 Maintenance 生成任务。**本页只有 GET 请求，不写任何数据。**

### 9.1 三种模式

顶部分段按钮（`Provider bars` / `Market bars` / `Economic`），面板右上徽章随之变为 `Raw provider_bars` / `Derived market_bars` / `Economic observations`：

| 模式 | 查什么 | 关键字段（默认值） |
|---|---|---|
| `Provider bars` | 原始 provider 行情 | `Provider`（`fixture`）、`Symbol`（`BTCUSDT`）、`Timeframe`（`1d`） |
| `Market bars` | 派生 canonical 行情 | 上面三项 + `Recipe`（自动选第一个已发布 recipe）、`Recipe version`（`1`）、`Price basis`（自动选第一个已发布基准） |
| `Economic` | 宏观序列 | `Series ID`（`PAYEMS`）、`Query mode`（`Current` / `Point in time`）；选 PIT 后出现 `As-of timestamp`（默认 `2026-09-10T00:00:00Z`） |

公共控件：`Start` / `End`（日期）、`Page size`（`1,000` / `500` / `100` / `25` / `2`，默认 1000）。
提交按钮文字随模式变化：**`Load bars and coverage`**（bars）/ **`Load market bars`**（market）/ **`Load observations`**（economic）。bars 模式一次提交会**同时**拉取数据（`/bars`）与覆盖度（`/provider-bars/coverage`），按钮文案因此同时提到两者；`Published coverage` 面板上的 `Load coverage` 是另一件事，只探测覆盖度、不取数据。

> 默认值是测试用的（`fixture` / `BTCUSDT` / `UI_TEST`），真实查询请改成生产 provider 与标的（如 `dukascopy` / `EURUSD`）。

### 9.2 查询结果

一次提交会**并发**发出两个请求（数据 + 覆盖度），任一失败整页进入错误态，数据也不显示。

- **覆盖摘要条**：`Scope` / `Readiness` / `Gap count` / `Ready intervals` / `Rows`（economic 模式为 `Scope` / `Rows` / `First` / `Last` / `Readiness`）。
- **结果面板**（`Snapshot`）：三个徽章 —— snapshot id（无则 `no snapshot`）、schema 版本（无则 `schema unknown`）、本页行数；表格列：
  - 行情：`Timestamp` / `Open` / `High` / `Low` / `Close` / `Volume`；
  - 宏观：`Observation` / `Value` / `Release` / `Frequency` / `Units`。
- **`Query warnings`**：例如把 `unbounded_query` 翻译成 `Unbounded query: keep cursor paging or narrow the window.`
- **翻页**：`Previous` / `Next`，中间显示 `Page N · M rows`；底部提示 `Cursor paging is bound to this selector and page size; changing a filter restarts at page 1.`

### 9.3 覆盖率与缺口面板（`Coverage` / `Ready intervals and gaps`）

- 明细字段：`Coverage scope`、`Readiness status`、`Gap count`、`Ready intervals`、`Rows in selector`、`Observed window (UTC)`（economic 为 `Observation window`）；market 模式额外有 `Recipe`、`Recipe status`、`Price basis`、`Input snapshots`；有则追加 `Quality status`、`Latest complete boundary`。
- ready intervals 列表：`ready` 徽章 + `start → end` + `half-open interval`。

**为什么经常看到 `Not published` / `Not evaluated by this response`**：治理字段不是每次都有 —— 原始行情的详细覆盖只在特定条件下计算（如 dukascopy + `1m` 且同时给了 start/end），派生行情要求 recipe 已注册且给了窗口，宏观覆盖本身只有行数与首末日期。**这是后端如实回答"我没有这项数据"，不是页面故障。**

要点：

- `Readiness` 取值 `ready` / `degraded` / `not_ready`；界面上 `degraded` 与 `not_ready` 都显示红色徽章，**只能靠文字区分**。
- 若后端只返回摘要，徽章显示 `Readiness unknown (summary only)` 并附说明，不会假装 ready。
- 覆盖度是**按 selector 聚合**的，与当前页的行无关。

### 9.4 把查询交接给维护工作台（`Create task from coverage`）

点击后按模式生成草稿并跳到 Maintenance：

| 模式 | 生成的 run kind |
|---|---|
| `Provider bars`，有缺口 | `gap_repair` |
| `Provider bars`，无缺口 | `backfill` |
| `Market bars` | `derive` |
| `Economic` | `ingest` |

窗口优先用表单的 `Start`/`End`，否则用覆盖度发布的观测区间；`Run scope` 固定为 `maintenance`。按钮旁会先说明"将会规划什么"：`Would plan gap_repair for … over …`，并附 `Coverage reports N gap(s), so the plan is a gap repair.` 与 `The Maintenance workspace revalidates coverage and capacity before queueing.`

若屏幕上还没有 selector 或没有有界窗口，会提示：`No bounded window is on screen, so … cannot be prefilled for …. Set Query start date and Query end date, or load coverage that publishes an observed window.`

### 9.5 本页没有的能力（避免空找）

**没有**导出/下载、复制到剪贴板、图表、行点击详情抽屉、取消查询、保存查询模板。需要这些请走 API（`docs/api-and-webui-contract.md`）。

---



## 10. Operations（运维）

**用途**：平台自身的运行面 —— 队列、worker 是否活着、容量趋势、历史 receipts、写审计。**这里回答"平台是不是在正常工作"，而不是"数据对不对"。**

### 10.1 顶部四个指标卡

| 卡片 | 内容 | 备注行 |
|---|---|---|
| `Capacity free` | 剩余空间百分比 | `X GiB free`；warning/critical 时变色 |
| `Queue depth` | 排队深度 | `oldest Ns`（最老任务等了多久） |
| `Dead letters` | 死信总数 | `N retry attempts` |
| `Temporary backups` | 临时备份数量 | `snapshot fresh` / `stale` / `unknown` |

### 10.2 各面板

1. **Maintenance queue**（`Ledger queue`）：徽章 `Queue empty` 或 `N queued`；三个计数 `Queued`（等待 worker）/ `Running`（in-flight）/ `Completed`；`Oldest queued (UTC)`（无排队时显示 `No queued job waiting`）；`Job states`（`status=count`）；`Runs by status` 芯片行。右上 `Refresh →` 只重读，不做任何写。底部说明：计数直接读自 run ledger 与 job queue，**不做估算**。
2. **Worker activity**（`Liveness`）：徽章 `heartbeat fresh|stale`；`Heartbeat age`、`Heartbeat status`、`Freshness limit`（秒）、`Observed (UTC)`；`In-flight jobs (N)` 列表（job id、run id、attempt 次数）。
   - `stale` 会额外显示：`The heartbeat is older than Ns, so queued work may not be picked up.`（队列不动的第一嫌疑人）
   - `unknown` 显示：`No heartbeat has been recorded yet, so liveness is unknown rather than assumed healthy.`
3. **Capacity history**（`Recorded measurement`）：`Live measurement` 区块（进度条 + `Status` / `Free ratio` / `Warning threshold` / `Critical threshold` / `Free space` / `Measurement source`）+ `Recorded transitions (N)` 表（`Event` / `Recorded (UTC)` / `Free ratio` / `Status` / `Thresholds`）。
   - 注意 `Measurement source`：若显示 `Pinned by the acceptance harness (fixed_measurement)`，说明这是验收环境**钉住的确定性数值**，不是真实磁盘读数（此时会额外给出说明）。生产应为 `Reported by the API as a live measurement (fixed_measurement = false)`。
   - 只有监控**实际记录**到的迁移事件才出现在表里：页面优先显示 API 的 `note`（生产实测为 `Only capacity transitions the monitor recorded are shown; no history is interpolated.`），API 未给时回退到 `Only capacity transitions the monitor recorded are listed.`
4. **Runtime**（`Deployment identity`）：`Deployment` / `Version` / `Source commit` / `Read path` / `Write path` / `Worker heartbeat`。身份信息全部来自 readiness 信封，控制台不会显示它没被告知的 commit。
5. **Capacity and recovery**（`Storage protection`）：容量进度条 + `Total` / `Used` / `Warning threshold` / `Critical threshold` / `Latest backup` / `Latest recovery drill`；下方 `Latest receipt per action` 列出**API 实际记录的动作**（如 `backup`、`backup_verify`、`restore`、`recovery_drill`、`capacity_check`、`retention_audit`、`scheduler_tick`、`deployment_stage`、`deployment_activate`、`deployment_rollback`、`deployment_runtime_failure`、`monitor`、`derived_market_bars_maintenance`、`real_release_webui_acceptance`、`post_release_rehearsal`），每条给出 `result`、完成时间、`deployment`、引用路径与关键字段；未记录的动作显示 `No receipt recorded`。再往下是 `Recent receipts (N)`。
   - **只渲染 API 报告的动作用途**：既不把已存在的记录误报为缺失，也不会出现平台从不写入的"幽灵动作名"。
   - 若 receipt 索引不可用，会显示 `Receipt index unavailable` 并明确写着 `This is a capability gap, not an empty success state.`
6. **Active alerts**（`Aggregated signals`）：聚合三类信号 —— 容量非 ok、存在 active 死信、operational snapshot 非 fresh；都正常时显示 `No active alerts`。
7. **Write audit trail**（`Append-only ledger`）：列 `Time (UTC)` / `Action` / `Actor` / `Task` / `Runs` / `Kind` / `Scope` / `Dataset` / `Selector` / `Outcome`（`queued`、`rejected`、`protected` 等，带 code）/ `Message`，默认最近 50 条。
   - `Actor` 是**调用者的不可逆指纹**（或网关注入的 `X-Operator`），**绝不是 API key 或凭据**。
   - 被拒绝、被容量保护的提交同样入账，所以"我看到提交失败了，但审计里没有"这种情况不该发生。
8. **Queue ingest**（`Authorized command`）：一个精简的排队表单 —— `Run scope` / `Provider` / `Symbol` / `Asset class` / `Timeframe` / `Start` / `End` → `Review ingest` → 二次确认 `Queue ingest run?`（文案含 scope、provider、symbol、asset class、timeframe 和日期区间）。
   - 前端校验：Provider/Symbol 必填（`Provider and symbol are required.`）；开始日期不能晚于结束日期（`Start date must not be after end date.`）。
   - **日常补数建议走 Maintenance 页**（有预览、有覆盖率、有模板）；这里的表单是运维应急入口，没有预览面板。
   - 保护态下按钮禁用并提示 `Capacity or readiness protection is active. Reads and recovery remain available.`

### 10.3 常见用法

| 你想知道 | 去哪看 |
|---|---|
| 任务排队了但没跑 | `Worker activity` 的 heartbeat 是否 `stale`；`Maintenance queue` 的 `Queued` 与 `Oldest queued` |
| 磁盘还够不够 | `Capacity free` 卡 + `Capacity history` 的 `Recorded transitions` |
| 谁在什么时候提交了什么 | `Write audit trail`（按时间、action、`Outcome` 过滤眼球即可） |
| 备份/恢复/发布最近一次是什么时候 | `Capacity and recovery` 的 `Latest receipt per action` |
| 现在跑的是哪个版本 | `Runtime` 区块（与侧栏底部的版本号一致） |

---

## 11. 端到端剧本（可直接照做）

### 剧本 A：补一段缺失的 1m 行情（gap repair）

1. Explorer 页查到缺口（或直接知道要补的区间）。
2. Maintenance → run kind 选 `Gap repair` → provider `dukascopy`、symbol `EURUSD`、timeframe `1m`、`Run scope=Production`、填 Start/End。
3. `Validate and preview`：确认 `Windows` 数量合理、`Gap count` 与预期一致、容量卡不是 protected。
4. `Confirm and queue` → 在 `Submitted tasks` 等终态。
5. Runs 页查该 run：看 `Windows` 是否全部成功、`Manifest` 是否 `published`、`Findings` 有几个。
6. 若 `Outcome=degraded`，读降级原因决定是否再补。

### 剧本 B：生成 1d 派生行情（derive）

1. Maintenance → `Derive` → provider/symbol 与你已有的 1m 数据一致 → `Recipe` 选 `utc-24x7-1m-to-1d-ohlcv`、version `1`、`Price basis` 按需。
2. 预览会显示输入快照与 `input part(s)`；确认快照是你想要的输入。
3. `Confirm and queue`。完成后在 Data catalog / Explorer 里查 `market_bars`。

### 剧本 C：只校验不落地（quality / parity）

1. Maintenance → `Quality check`（或 `Parity check`）→ 填 selector → 预览 → 提交。
2. 这类 run **不会发布 canonical part**，Runs 页的 `Manifest` 会显示无需 manifest 的说明，`Findings` 才是产出。
3. 去 Quality 页处理新产生的 findings。

### 剧本 D：处理一条 finding

1. Quality 页筛 `Finding state = open` → 打开行看 severity / code / message / `Occurrences` / `Selector`，以及 `Recorded coverage` 与 `Run linkage`（点 `Open run` 看上报它的那次运行）。
2. 判断"已知且可接受" → `Acknowledge finding`；判断"其实已修好" → `Mark resolved`；判断错了 → `Reopen finding`。
3. 需要真修：
   - 页面给了 `Create repair task`（`provider_bars` 的覆盖类缺陷 → `gap_repair`；`market_bars` 的可修缺陷 → `parity`）→ `Create repair task` → 核对预览 → `Confirm and queue` → 在排队结果块点 `Link run to finding` → `Mark resolved by run`；
   - 页面显示 `No bounded repair task is defined for …`（blocked）→ 点 `Open maintenance workspace`，手动按 §6.4 规划（**不会带草稿过去**）。
4. 修完回头看该 run 的 outcome：只有 run 真的成功，`resolved` 才算名副其实（关联只是记录，不会自动回滚状态）。

### 剧本 E：失败 run 的处置

1. Overview 的 `Degraded and failed runs` 或 Runs 页筛 `failed` / `dead_letter`。
2. 打开详情读 `Attempt errors`：区分"偶发网络问题"（直接 `Retry`）和"参数错误"（改参数重新提交，重试只会再失败一次）。
3. 死信确认无法修复 → `Acknowledge` 记账，并在 Quality/审计里留下依据。

### 剧本 F：容量告警

1. Overview 横幅变红 → `Review capacity →`。
2. Operations 看 `Capacity free` 与 `Recorded transitions`，确认是趋势还是突增。
3. 清理临时文件/旧 release（按 `docs/operations-runbook.md`，**绝不**删除 canonical parts、manifests、终态 receipts 或 ledger）。
4. 回到 Maintenance 重试被 507 挡下的任务。

### 剧本 G：确认"生产到底是哪个版本"

1. 侧栏底部版本 · deployment id。
2. Operations → `Runtime`：`Version` / `Source commit` / `Deployment`。
3. 与 `docs/current-state.md`、`docs/release-checklist.md` 里记录的 tag 对齐（本次为 `0.4.1` / `49ddbc5…` / `49ddbc55361d-bd11ad0e`）。

---

## 12. 状态与徽章速查

| 徽章 / 文案 | 出现位置 | 含义 |
|---|---|---|
| `API ready` / `API unavailable` | 侧栏底部 | API 进程是否就绪 |
| `Snapshot fresh` / `stale` / `unknown` | 侧栏底部 | 运维快照新鲜度 |
| `ok` / `warning` / `critical` | 顶部、容量相关处 | 容量状态（warning/critical 只保护**写**） |
| `Writes available` / `Writes protected` | Maintenance | 当前是否能排队任务 |
| `Queue empty` / `N queued` | Operations | 队列是否有积压 |
| `heartbeat fresh` / `stale` / `unknown` | Operations | worker 是否在跑 |
| `published` / `not_applicable` / 其它 | Runs | 是否产出 canonical manifest |
| `pass` / `degraded` / `failed` / `dead_letter` | Runs、Overview | 运行结果 |
| `queued` / `running` / `pass` / `failed` / `degraded` | 维护跟踪徽章 | 本次提交的推进状态（`queued` ≠ 成功） |
| `open` / `acknowledged` / `resolved` | Quality | finding 处理状态 |
| `Not authorized` | 任意写操作 | 401：缺/错 API key |
| `Write protected` / `Blocked by capacity` | Maintenance、Operations | 507：容量保护 |
| `Blocked by validation` | Maintenance 预览 | 422：参数或矩阵不合法 |

---

## 13. 故障排查

| 症状 | 先查 | 处理 |
|---|---|---|
| 页面全是 `Unable to load data` | 侧栏 `API unavailable`？ | 服务未起或隧道断了；本机 `curl -s localhost:18380/api/v1/health/ready` |
| 点提交提示 `Not authorized` | `Session access` 是否填了 key | 填 key 后重试（刷新页面会丢 key，属预期） |
| 任务排队很久不动 | Operations → Worker activity | `stale` → worker 没在跑；再查 `Oldest queued` 与死信 |
| 提交被 507 拒绝 | 容量徽章、Operations 容量面板 | 先释放空间；读操作不受影响，可继续排查 |
| 预览 `Blocked by validation` | 红色 `Validation errors` 列表 | 按字段与 `code` 改参数；矩阵不允许的组合界面已禁用，遇到说明数据来自旧界面缓存 |
| 找不到某个 dataset | Data catalog / Explorer | 生产只有 `provider_bars`、`market_bars`、`economic_observations` 三个 |
| Runs 翻了页却看到重复/空 | 是否中途改了筛选 | 游标绑定筛选条件，改筛选会回到第 1 页（预期行为） |
| `fixed_measurement` 提示 | Capacity history | 这是验收环境钉住的数值，不是生产磁盘读数 |
| Receipts 显示 `No receipt recorded` | 该动作是否真发生过 | 平台只展示实际记录的动作；没发生过就是没记录，不会补造 |
| 页面样式/功能像旧版本 | 侧栏底部版本号 | 与 `Runtime` 区块、发布记录核对；必要时重新加载（浏览器缓存） |

---

## 14. 安全与边界（务必知道）

1. **控制台只通过 `/api/v1` 读写**，从不直接打开 Parquet 或 SQLite；因此你能在界面上看到什么，完全等于 API 契约允许什么。
2. **不显示凭据**：审计里的 `Actor` 是不可逆指纹；页面上永远不会出现 API key、provider 密钥或个人路径。
3. **历史不可改写**：终态 run、receipt、审计都是追加式；界面没有"删除/编辑历史"这类按钮（有的话就是 bug）。
4. **容量保护只挡写**：507 期间读取、查询、诊断照常可用——这是刻意设计，避免把"磁盘紧张"误报成"平台全挂"。
5. **模板与 key 都只在浏览器内存/localStorage**：模板不含凭据，key 不进 localStorage。
6. **演练与生产分开**：验收/演练请用 `acceptance` scope，别把演练 run 混进 `production` 统计口径。

---

### 8. Production plans（生产计划）

统一调度与生产任务的唯一界面。顶部是调度器状态条：派发开关（唯一开关是运维动作 `POST /operations/scheduler/actions`）、心跳与租约、
当前到期数、**新发布型派发是否允许**（容量 `critical` 时拒绝，`warning` 只挡无人值守补齐）与按提供方的退避。注册表列出计划的选择器、
产物、`phase`/`health`（含 `block_reason`）、下次运行与当前轮次，并提供按健康度筛选。操作列可暂停/继续/立即执行/归档/删除（破坏性操作需确认）
与**编辑**：编辑带 `definition_version` 乐观锁，名称等展示字段就地更新、不产生新版本，冲突时提示重新加载。向导从 `/capabilities` 播种，
先预览再保存，"保存为暂停"与"保存并启用"是两个独立动作。详情面板显示调度器**实际持久化**的边界（raw 规划到哪、提供方已提供到哪、
完整到哪、派生游标、欠多少重算、未解决缺口、等待输入的 bucket）并注明"这是记录，不是实时新鲜度"。

## 15. 想深入时读什么

| 文档 | 内容 |
|---|---|
| `docs/api-and-webui-contract.md` | 每个接口的字段、游标、鉴权与审计策略 |
| `docs/current-state.md` | 当前生产 deployment、tag、未完成事项 |
| `docs/operations-runbook.md` | 部署、健康检查、容量、回滚的运维步骤 |
| `docs/dataset-contract.md` | 三个数据集的不变量与分区规则 |
| `docs/specs/2026-09-13-webui-data-workbench-v0.4.md` | 本版 WebUI 的设计规范（工作台、契约、验收） |
