# P0 独立预览阶段验收

日期：2026-09-15
状态：用户阶段验收通过；独立评审修正和完整本地 gate 通过，等待 hosted CI

## 当前验收卡

- 阶段：P0 / E01；PV01、PV02、PV04、PV05、PV07，PV03/PV06/PV08 基础范围。
- 预览：`http://127.0.0.1:25345`；远程访问时转发 UI 25345 与 API 25344。
- 身份：`codex/p0-isolated-preview` / clean implementation commit `a573675`；observed_at `2026-09-15T08:29:25Z`（阶段验收记录，精确运行身份以 `status` 为准）。
- 环境：分支预览，fixture；数据与日志保留在 ignored `.preview/p0-eurusd/`。
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
