# P0 独立预览阶段验收

日期：2026-09-15
状态：用户阶段验收、独立评审、完整本地 gate 与 hosted CI 通过；PR #123 已合并

## 当前验收卡

- 阶段：P0 / E01；PV01、PV02、PV04、PV05、PV07，PV03/PV06/PV08 基础范围。
- 预览：`http://127.0.0.1:25345`；远程访问时转发 UI 25345 与 API 25344。
- 身份快照：main 集成预览 / commit `68f4b647` / dirty=false；observed_at `2026-09-15T10:10:15Z`。实时身份以开发指南中的 `status --base /home/quant/repos/.preview` 读回为准。
- 环境：main 集成预览，fixture；数据与日志保留在 `/home/quant/repos/.preview/p0-eurusd/`，与代码 worktree 解耦。
- 操作：打开 UI；核对顶部环境条；初始化至少 12 位密码并登录/退出；观察顶部 scheduler 心跳；打开 `http://127.0.0.1:25344/docs`。
- 预期：明确显示“预览 · 模拟数据”、id/commit/dirty/mode；登录写入独立 cookie/auth 库；调度显示有效派发且心跳变化；API 文档独立可用。
- 限制：P1 才交付 shadcn-admin 主线和详情路由；完整 fixture task→scheduler→worker→发布→读回在 P2/P3；live sandbox 在 P2；不联系真实 provider，不外发告警，不部署生产。

## 实测记录

| 验收 | 当前证据 | 状态 |
| --- | --- | --- |
| PV01 | `start` 在 25344/25345 启动 API、worker、scheduler、Vite，四进程健康 | 已验证 |
| PV02 | `p0-eurusd` 与 `p0-parallel` 使用不同端口、ledger、auth cookie；停止后者不影响前者 | 已验证 |
| PV03 | fixture allowlist 和告警关闭已实现；完整产品链路留 P2/P3 | 基础实现 |
| PV04 | 第二预览经 Vite 同源代理初始化、登录、退出成功；其 cookie 访问第一预览返回 401 | 已验证；用户复验通过 |
| PV05 | stop 后同端口重启，保留 auth/ledger/data；按 PID 启动时间、cwd、随机 token 核对停止目标 | 已验证；失败清理与拒绝项由自动检查覆盖 |
| PV06 | API readiness 返回 commit/environment/mode/dirty，页面环境条显示身份和真实 scheduler 心跳；路由留 P1 | 基础实现 |
| PV07 | 已向维护者提供 URL、SSH 转发、5 步操作、预期和限制；预览保持运行 | 已交付 |
| PV08 | 默认只允许 fixture；live 模式尚未实现 | 基础隔离 |

## 用户反馈

- 2026-09-15：维护者确认环境标识检查通过。
- 2026-09-15：首次密码设置返回 `origin not allowed`；已修正 Vite 代理 Origin/Host 语义，并在第二预览实测初始化、登录、退出通过，等待维护者复验。
- 2026-09-15：维护者复验初始化、登录、改密和退出通过。auth 数据库隔离属于技术证据：两个预览使用不同 auth/ledger 路径和 cookie 名，跨预览 cookie 返回 401，重启保留认证状态；页面不暴露数据库文件。
- 2026-09-15：维护者未在“Operations”找到 scheduler 心跳；原提示位置有误，既有 scheduler 面板位于“生产计划”。为降低发现成本，预览顶部改为每 5 秒显示有效派发与实际心跳；维护者复验持续心跳通过。
- 2026-09-15：维护者询问预览持久化边界；已说明默认 `.preview/p0-eurusd` 不进 Git，删除 worktree 会删除运行数据，交接前保留现有环境；跨 worktree 保留可改用外置 `--base` 或备份。

## 合并前验证

- `DATACENTER_PYTHON=.venv/bin/python bash scripts/ci.sh all`：在评审修正 commit `db7f7b3` 通过；487 passed、5 skipped；Node 22 Web build、桌面/移动浏览器全旅程、服务重启验收通过。
- `NODE_ENV=production npm --prefix webui ci --include=dev`：通过；确认 #106 场景仍安装锁定的 Vite/TypeScript/Playwright 开发依赖。
- `.venv/bin/python scripts/dev_preview_acceptance.py --python .venv/bin/python`：通过；four_process_start、parallel_isolation、same_origin_auth、cookie_isolation、independent_stop、restart_retention。

## 合并与交接

- PR：[#123](https://github.com/hezongyi/market-data-center/pull/123)，head `2c73387` 的 Python 3.10/3.11/3.12、Web browser 与 required `verify` 均通过；2026-09-15 合并为 `7019dc3`。
- 本地基线快照：`/home/quant/repos/market-data-center-latest` 的 `main` 于上述观测时间已 fast-forward 到 `68f4b647`；最终当前值由 `git rev-parse HEAD origin/main` 核对。
- 运行预览：原 auth/data 保留，已在相同端口重启；运行身份由 `status` 读回，不以本历史验收卡持续追写每个后续文档提交。P0 交接后不自动进入 P1，不部署生产。
