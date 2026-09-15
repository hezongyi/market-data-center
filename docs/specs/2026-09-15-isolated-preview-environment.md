# 独立预览环境规范

日期：2026-09-15
状态：approved；P0 基础范围 implemented / user accepted
来源：维护者确认的 EURUSD 优先闭环与阶段验收方案。P0 已交付 PV01、PV02、PV04、PV05、PV07；PV03、PV06、PV08 的完整范围分别留待 P2/P3、P1、P2。

## 1. 目标与环境

Draft PR 尚未完成时，用户即可在运行中的完整 WebUI 操作验收。分支预览、main 集成验收、生产是不同环境；不能依靠生产发布来展示开发中的界面。

起步采用本机 worktree+独立端口+受监督进程，不引入云环境平台。预览始终使用独立 API、worker、scheduler、auth、canonical、ledger、evidence、backup 与日志。浏览器自动验收另起临时环境，不重置用户正在试用的预览。

## 2. 管理入口契约

已提供 `bash scripts/dev-preview.sh start|status|stop --id <preview-id>`。持久预览位于仓库 ignored `.preview/<id>/`，也可显式指定受控 preview base；用户操作数据保留，stop 不删除目录。默认目录仍随整个 worktree 删除，需跨 worktree 保留时必须使用外置 base 或预先备份。

- start：校验配置与端口，显式构造环境，启动 API/worker/scheduler/Vite，健康通过后返回访问卡。默认 fixture；存在同名环境时校验身份后复用或解释拒绝，不覆盖。
- status：报告各进程是否存活、实际 checkout/commit/dirty 标识、URL、数据模式、调度进程开关/账本开关/有效派发、数据根、日志与启动时间。身份不一致显示失败，不借用生产 readiness。
- stop：仅停止已核实属于本 preview-id 的进程组，校验 PID 与启动身份；不能按公共端口或模糊进程名杀进程。保留数据与日志；显式数据清理为独立操作。
- 切换版本：同一预览不静默改变代码或重置数据；先显示新旧身份和必要重启。用户验收完成前保持可访问，结束后告知停止与保留位置。

## 3. 隔离与连接

- 不继承生产 `.env.local`、机器级 env、deployment manifest、webhook 或凭证。允许最小工具链环境，业务配置采用显式白名单；真实源的代理/凭证另行显式提供，不打印秘密。
- 所有路径启动前 resolve 并检查符号链接；拒绝任何生产数据根、其父级/子级或重叠根。拒绝 workspace 根和共享上层目录作为数据目录；没有明确定义根时失败，不回落 Settings 的生产默认值。
- API/worker/scheduler 三者使用同一份预览配置，独立 auth 数据库也在预览根下。并行预览不得共享 ledger、凭证或端口。
- Vite 的 `/api` 目标可配置，预览入口必须显式注入并校验对应 API 身份；不能默认指向现有 18380。设置 strictPort，端口冲突明确失败或由管理入口重新分配，不能偷偷换端口。
- 默认绑定 loopback。远程用户通过可用的受控通道/SSH 转发访问，交付卡提供实际可达说明；不得假设用户浏览器和服务器共享 localhost。
- 同源代理保留正确 Host/Origin/cookie 语义。隔离 HTTP 开发使用仅作用于预览的 cookie 配置；生产仍使用 HTTPS secure cookie。初始化凭据不放公开文档、构建产物或提交记录。
- Cookie 不按端口隔离：两个 localhost 端口不能共享同名会话 cookie 来冒充认证隔离。实现须采用独立预览主机名或仅预览可配置的独立 cookie 名等实际隔离方式，并验证两个页面同时登录/退出互不干扰；不改变生产 cookie 名称和权限语义。
- scheduler 在 fixture 环境可实际派发；其账本与进程有效开关都必须可见，不能仅启动 shadow 进程来宣称定时运行已可验收。

## 4. 数据模式

fixture 为默认模式：允许受控的本地 provider adapter 和固定样本，禁止真实 provider/webhook 访问。提供确定性的正常、源缺口、故障后恢复场景，页面标注“预览 · 模拟数据”。仅测试环境启用 adapter，不把未知品种豁免加入生产默认配置。

真实源沙箱为显式 live 模式：仅 Dukascopy、EURUSD（第二任务阶段增加 GBPUSD），限定窗口、请求数、磁盘/运行预算；预算耗尽停止新增拉取并解释。数据仍写预览根，不导入生产计划或修改生产 writer。凭证、联网与范围沿用用户明确授权，不能由等待超时自动启用 live 模式。

首期不从生产直接挂载可写 canonical，也不要求克隆全历史。若以后需要生产样本，应以独立、只读提取流程定义，不能混进默认 start。

## 5. 验收与交付

| 编号 | 通过条件 |
| --- | --- |
| PV01 | 一条 start 启动完整四进程并给出可访问 UI、API docs、identity、模式和日志；没有生产服务也能工作 |
| PV02 | 并行两个预览，写入与启停互不影响；端口占用、错误根、符号链接到生产、意外 env 都明确拒绝 |
| PV03 | fixture 操作走真实任务→scheduler→worker→发布→HTTP/UI 读回；无真实源/告警外发 |
| PV04 | 登录、退出、初始化及 cookie 写操作在预览实际可用；两个预览并行登录/退出不串会话，生产认证设置不变 |
| PV05 | stop/重启保留数据与计划；启动失败清理已启动子进程；PID 复用不误停其它进程 |
| PV06 | 页面显示准确环境/commit/dirty 和有效派发状态；直达/刷新详情 URL 可用，后续 Router 引入时补相应验收 |
| PV07 | 用户获得 3–5 步验收卡和实际访问方法；预览保留到阶段交接，无未声明自动重置 |
| PV08 | live 显式选择后仍只写沙箱，范围/预算可核对；模拟证据与真实源证据分开 |

P0 先验收隔离与既有界面可用，完整 raw→derived 场景随 P2/P3 补齐 PV03；Router 相关 PV06 随 P1 交付。不得因此提前将所有 PV 标为通过。实现 PR 同步在开发指南填写已经验证的命令、访问方法和限制。
