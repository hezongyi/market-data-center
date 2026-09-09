# 运行维护落地与验收

本轮范围对应用户要求的六项后续建议。以下状态必须由实际执行证据更新，代码存在不等于生产验收通过。

| 项目 | 实现与验收要求 | 状态 |
| --- | --- | --- |
| 代码提交与 CI | 提交已有可验证改动；CI 安装依赖、测试、前端构建、隔离服务 smoke | 待完成 |
| 定时 provider 验收 | systemd timer、请求间隔、失败告警文件、receipt 保留策略；真实 timer 触发 | 待完成 |
| 进程超时与恢复 | 超时杀死执行进程，重启不重复领取；验证无迟到写盘、心跳持续更新 | 待完成 |
| 失败管理 | failed/dead-letter 查询、授权重试、原 receipt 保留、UI 操作 | 待完成 |
| 下游接入 | macro-market-lab 只读 adapter parity，迁移低风险数据预览；经济 PIT 不冒充兼容 | 待完成 |
| 保留、回补、日志指标 | receipt 归档保留、canonical 保留审计、显式日期回补、结构化 request/run 日志及指标 | 待完成 |

当前仓库没有配置 Git remote。CI 将提供可在本机运行的同一入口，并保存本地结果；不能据此宣称 GitHub 托管 CI 已执行。

保留策略只自动处理定时验收 receipt 与临时运行产物。canonical 历史数据与原始 run receipt 不自动删除；数据回补通过正式入队接口产生新 run 和新 part。
