# shadcn-admin 本地参考与来源

- 官方仓库：https://github.com/satnaing/shadcn-admin
- 固定提交：`e16c87f213a5ba5e45964e9b67c792105ec74d26`
- 参考 package 版本：2.2.1；采用日期：2026-09-15。
- 许可证：MIT；复制到项目的文件保留版权与许可声明。
- 本机工作区参考目录：`/home/quant/repos/references/shadcn-admin`（不作为程序运行时依赖）。

本地保存完整 Git 仓库，工作树固定到上述提交。该目录在应用仓库外，不将整个上游或它的 node_modules 纳入数据中心版本控制。其它机器可以在任意目录克隆相同提交，agent 从此记录找到来源；离线缓存缺失时明确报告，不偷偷改用其它版本。

本次核验：完整 clone（非 shallow），HEAD 与上述提交一致，工作树干净，含原始 MIT LICENSE，本地约 6.3 MiB。没有安装模板 npm 依赖或运行演示站。

```bash
git clone https://github.com/satnaing/shadcn-admin.git /path/to/references/shadcn-admin
git -C /path/to/references/shadcn-admin switch --detach e16c87f213a5ba5e45964e9b67c792105ec74d26
git -C /path/to/references/shadcn-admin rev-parse HEAD
```

优先读本地 README、LICENSE、package.json、src/components/layout、src/components/ui、src/features/tasks 和 src/features/auth。上游规范仅作参考，项目需求以当前 spec 和 AGENTS 为准。

不默认 git pull 或跟踪所有上游变化。需要修复时明确核对新 commit、说明差异并更新引用记录；复用复制清单按需合并。初次组件引入 PR 创建 `webui/vendor/shadcn-admin/PROVENANCE.md` 与许可证副本，逐项列出上游路径、本地路径、修改原因和版本，不能提前声称已复制组件。

本地 clone 可避免反复联网读取源码；首次安装 npm 包或缺失浏览器仍可能需要网络。clone 不代表模板依赖已安装、演示站已运行或 UI 已验收。
