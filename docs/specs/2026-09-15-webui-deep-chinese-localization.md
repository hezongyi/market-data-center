# WebUI 深度中文化规范

日期：2026-09-15
状态：approved
排期：paused（2026-09-15，全站铺开暂停）。新 EURUSD 主线页面的中文、状态和交互随[产品与 UI 基线](2026-09-15-eurusd-first-product-baseline.md)同时交付；其它页面保留兼容，本文不再自动产生全站改造任务。
前置版本：WebUI v0.5 核心中文界面
关联文档：`docs/specs/2026-09-13-webui-v0.5-usability-and-access.md`

## 背景

v0.5 已覆盖导航、工作区标题、主要操作控件、核心状态、关键 ID 和时间展示。复杂详情、审计、诊断、错误堆栈、provider 专有字段仍可能显示英文。继续在同一迭代中零散修改会增加文案不一致和回归风险，因此单独建立深度中文化规范。

## 目标

1. Catalog、Explorer、Runs、Quality、Maintenance、Operations 的所有用户可见文本均通过统一 i18n 资源渲染。
2. 覆盖详情抽屉、确认框、错误和空状态、筛选项、帮助提示、审计字段及动态 API 错误。
3. 中文与英文资源具有相同 key 集，切换语言后无需刷新业务数据。
4. 不翻译数据集 ID、provider 名称、状态协议值、错误代码、哈希和用户复制的原始内容。

## 实施要求

- 将页面文案从 JSX 中移入按页面分组的资源，禁止新增硬编码用户文案。
- 状态和错误使用语义 key 与参数插值，避免拼接半句英文。
- `aria-label`、`title`、placeholder、表格表头和按钮也必须走 i18n。
- 动态错误保留结构化 code，并为 message 提供中文模板和英文原文查看入口。
- 继续使用 `TimeDisplay` 和 `CopyId`，本规范不改变 UTC canonical 语义。

## 验收标准

- 默认 `zh-CN` 下六个工作区的可见文本扫描不再发现未登记英文文案。
- 切换 English 后同一页面的标题、详情、错误、空状态和辅助文本全部恢复英文。
- 1440px 和 390px 下浏览器断言覆盖每个工作区至少一个列表、详情和错误状态。
- i18n key parity 检查通过，动态错误 code、ID、哈希和 provider 原始值保持不变。
- Python、Node、完整 CI 与隔离浏览器验收通过。

## 交付拆分

1. 建立资源文件和 key parity 检查。
2. 完成 Catalog、Explorer、Runs。
3. 完成 Quality、Maintenance、Operations。
4. 增加默认中文扫描、英文切换和动态错误浏览器验收。
