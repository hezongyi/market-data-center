# Dukascopy baseline reconstruction plan

日期：2026-09-11  
目标分支：`dukascopy-provider-ingest-20260911`  
基线：`origin/main` / `v0.2.0`

## 目的

将旧 worktree 中尚未提交的 Dukascopy provider 实现，逐文件移植到已经完成 release、deployment 和 observability 验收的最新主线。旧 worktree 不作为整体 merge 来源，也不作为生产运行目录。

## 来源与目标

- 来源 worktree：`/home/quant/repos/market-data-center`
- 目标 worktree：`/home/quant/repos/market-data-center-latest`
- 目标基线 commit：以创建本分支时的 `origin/main` 为准
- 旧 worktree 中的 deployment、capacity、snapshot、backup、release 和 CI 改动不整体移植

## 允许移植的范围

首批只移植 Dukascopy adapter、registry、domain 字段、依赖、测试、acceptance 和对应 spec。每个文件移植后都要与目标主线 diff 核对，避免回退已接受的 release/deployment contract。

候选文件：

- `backend/src/data_center/connectors/dukascopy.py`
- `backend/tests/test_dukascopy_connector.py`
- `docs/specs/2026-09-10-dukascopy-provider-ingest.md`
- `backend/pyproject.toml`
- `backend/constraints/py310.txt`
- `backend/constraints/py311.txt`
- `backend/constraints/py312.txt`
- `backend/src/data_center/connectors/registry.py`
- `backend/src/data_center/domain/models.py`
- `backend/src/data_center/acceptance.py`

## 禁止整体覆盖的范围

以下模块以最新主线为 source of truth，除非出现明确的 Dukascopy 依赖冲突，否则不得从旧 worktree 覆盖：

- `backend/src/data_center/deployment.py`
- `backend/src/data_center/snapshot.py`
- `backend/src/data_center/capacity.py`
- `backend/src/data_center/operations.py`
- `backend/src/data_center/observability.py`
- `scripts/ci.sh`
- `.github/workflows/*`
- `deploy/systemd/*`
- release checklist、release receipt 和已有 deployment evidence

## 执行顺序

1. D0：确认 clean protected-main baseline、版本号和三个 Python constraints。
2. D1：移植 adapter 与 fake-provider contract tests，不访问真实网络。
3. D2：移植 registry/依赖/acceptance，验证通用 API → queue → worker → manifest → receipt → readback 链路。
4. D3：在 immutable release 上执行正式 Dukascopy acceptance；临时 canonical smoke 只能作为开发证据。
5. D4：先完成 legacy inventory 和容量门禁，再进行 bounded migration 与 consumer parity。capacity 为 warning/critical 时不启动 bulk migration。

## D0 记录

- [x] 目标分支从 `origin/main` 创建
- [x] 目标 worktree 初始 clean
- [x] 基线版本为 `v0.2.0` 之后的 protected-main commit
- [x] Dukascopy adapter、registry、acceptance、spec 和 contract tests 完成逐文件移植
- [ ] Python 3.10/3.11/3.12 locked install 验证（当前已验证 py311；本机 python3.10 缺少 ensurepip/venv，python3.12 未安装）
- [x] 本地统一 CI 与 snapshot benchmark 验证（py311）

## 当前证据

- `de15f2e`：移植 Dukascopy adapter、registry、依赖、constraints、acceptance、spec 和本计划。
- `bad88c4`：修正 acceptance 失败告警测试并通过格式检查。
- D1 contract tests：5 passed。
- 本地 `bash scripts/ci.sh all`：122 tests、Ruff、dependency/compatibility、secret scan、operations acceptance、10,000 receipt snapshot benchmark、Web build、browser acceptance 和 service acceptance 全部通过。
- Browser acceptance 使用隔离服务和 `EURUSD` 以外的 fixture 数据，不构成 D3 真实 provider 证据。
- Python 3.10 安装尝试因系统缺少 `python3.10-venv` 失败；未安装系统包，避免修改宿主环境。

## 删除旧 worktree 的前置条件

只有在 Dukascopy 改动已提交并合并到 protected `main`、目标 worktree 已同步、必要 receipt/patch 已保留且旧 worktree 没有独有未提交内容后，才允许删除 `/home/quant/repos/market-data-center`。
