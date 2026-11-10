# 单一版本源实施计划

**Goal:** 以后只修改 `fnmusic/__init__.py` 的 `__version__`，其余版本信息自动读取或渲染。

**Architecture:** setuptools 动态读取该属性；FPK 构建脚本导入该属性并渲染清单模板；管理服务渲染页面中的同一占位符，保留缓存参数和升级重连版本识别。README 链接版本源。

**Tech Stack:** Python、setuptools、FastAPI、pytest。

## 约束

- 当前版本仍为 0.1.25，不打包、不部署。
- 保留 FPK 源码完整性校验，包内 HTML 模板与仓库一致，由运行时渲染版本。
- 不改第三方运行时版本、历史设计记录和历史测试夹具版本。

## 执行与验证

- [x] 在 tests/test_updates.py 验证 setuptools 真实解析版本；在 tests/test_fpk_builder.py 模拟将来版本检查清单和桌面入口；在 tests/test_manager.py 模拟将来版本检查页脚、缓存参数、页面版本属性及根路径/网关前缀。
- [x] 运行这些测试并确认失败，再修改 pyproject.toml、scripts/build_fpk.py、packaging/fpk/manifest.in、fnmusic/manager.py 和 fnmusic/static/index.html。
- [x] README.md 链接唯一版本源，docs/github-build.md 改为只修改一处的说明。
- [x] 运行相关测试、完整回归、ruff、diff 检查，确认没有新增 FPK 或其他构建产物。

## 验收结果

- 先运行三个新增/调整的版本测试，确认旧实现因多处硬编码而失败；实现后相关七项测试通过。
- 完整回归：244 passed、1 skipped，另有一项现有 Starlette/httpx 弃用警告。
- ruff、JS 语法及 git diff 检查通过。没有生成安装包或发布产物。
