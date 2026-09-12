# 文档入口

阅读顺序：

1. [当前状态](current-state.md)：生产 deployment、数据 ownership、consumer 迁移矩阵和未完成事项。
2. [Operations runbook](operations-runbook.md)：部署、健康检查、维护、容量和回滚操作。
3. [现行 specs](specs/)：按日期和状态查看设计规范；`superseded` 文档仅作历史背景。
4. [API/WebUI contract](api-and-webui-contract.md)、[dataset contract](dataset-contract.md)、[ingest contract](ingest-contract.md)：接口和数据不变量。
5. [integration/macro-market-lab.md](integration/macro-market-lab.md)：macro-market-lab 的读取、维护 ownership 和回滚约定。

## 状态规则

Spec 状态统一使用：`draft`、`approved`、`in_progress`、`implemented`、`accepted`、`superseded`、`blocked`。
历史 receipt 不修改；如当前事实变化，只更新 `current-state.md` 和新增 receipt 链接。
