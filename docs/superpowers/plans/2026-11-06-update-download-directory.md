# NAS 安装包下载目录实现计划

**Goal:** 在软件更新页保存用户指定的 NAS 安装包目录，下载后显示完整文件路径，留空保留私有缓存。

**Architecture:** Updater 单独持久化 `update-settings.json`，复用 NAS 卷目录校验。自定义目录使用发布文件名；默认目录继续使用 `updates/update.fpk`。现有管理员和请求来源校验保护新增保存接口。

**Tech Stack:** Python / FastAPI / pytest / 原生 JavaScript。

用户已批准此方案，在当前 `feat/lx-first` 分支执行；不自动安装、发布或重启。

- [x] 在 `tests/test_updates.py` 编写并运行失败用例：目录持久化、默认恢复、自定义文件路径及权限、下载中禁止变更、校验失败保留已有文件。
- [x] 在 `fnmusic/updates.py` 实现 `save_directory(value)`、目录状态和下载目标选择；复用 `validate_directory`，保持共享目录权限，下载时重新校验目录。
- [x] 在 `tests/test_manager.py` 验证 `POST /api/update/settings` 管理员/来源校验、保存和读取。
- [x] 在 `fnmusic/manager.py` 接入接口和操作日志。
- [x] 在 `fnmusic/static/index.html`、`admin.js`、`admin.css` 增加独立目录表单、未保存提示、下载后路径显示。轮询保留未保存输入。
- [x] 更新 `README.md`，运行完整 pytest、Ruff、JavaScript 语法检查和 `git diff --check`，检查实际界面。

约束：仅允许已挂载 `/volN/` 下可写绝对目录；保存空值恢复默认；不移动/删除旧目录里的安装包；SHA256 通过后才发布完整文件；共享目录不 chmod；默认缓存仍为 0600，自定义完整包为 0644；新增 API 不提供任意文件下载。

独立审查复现了共享目录缓存/临时文件被替换为符号链接时的权限风险；已使用 O_NOFOLLOW 打开、同一文件句柄校验/fchmod、文件身份检查和句柄流式下载修复，新增对应回归用例。页面使用本机模拟发布和安装包验证保存、未保存输入保留、下载中禁用设置及完成路径显示；未连接真实 NAS 或发布安装包。

最终验证：完整回归 157 passed、1 skipped（既有环境相关跳过）、1 条既有测试工具弃用警告；Ruff、JavaScript 语法和 git diff --check 通过。

用户后续要求：不增加24小时清理；所有日志展示及导出按最新记录在前，启动失败堆栈内部保持顺序。推送前验证：完整回归160 passed、1 skipped、1条既有弃用警告；Ruff、JavaScript语法和差异检查通过。
