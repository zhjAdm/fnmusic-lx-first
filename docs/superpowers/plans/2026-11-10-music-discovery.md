# Music Discovery Implementation Plan

**Goal:** 实现已确认的两个发现歌单和管理页版本雷达。

**Architecture:** 使用独立 history.py 保存播放活动，discovery.py 负责推荐设置、探险和本地考古，versions.py 负责只读版本分组。现有 Playlists 和 app 负责官方协议适配，manager 与静态页面提供管理功能。

**Tech Stack:** Python 3.11、SQLite、FastAPI、原生 JavaScript/CSS、pytest。

## Global Constraints

- 只新增两个歌单，最多各 30 首，按用户隔离。
- 本地曲库通过官方会话检查访问权限，不直接暴露共享目录文件。
- 播放活动不代表听完；推荐不创建下载任务。
- 不打包、不推送，不覆盖原有未提交改动。

## 执行顺序（当前会话内逐项执行）

- [x] 1. tests/test_discovery.py：先验证播放记录持久化、旧日志导入、用户隔离和失败事件排除；实现 fnmusic/history.py 并接入 logs.activity。
- [x] 2. 同一测试文件覆盖三个探险档位、排除/去重、冷热候选混合和无画像兜底；实现 fnmusic/discovery.py 的设置和探险选择，接入 playlists.py。
- [x] 3. 测试本地考古每日稳定选择、30 天冷却和权限过滤；扩展索引只读查询，考古使用 Official.find_local 获取官方歌曲对象，app.py 保留官方 GUID 和格式。
- [x] 4. tests/test_versions.py：验证 Live/伴奏/重制等后缀、同名不同歌手隔离、普通括号不剥离、当前目录过滤及分页；实现 fnmusic/versions.py。
- [x] 5. tests/test_manager.py 和 tests/test_app.py：覆盖管理权限、设置校验、官方接口分页/元信息/本地播放记录；接入管理路由和静态页面。
- [x] 6. 更新 README.md 和 docs/development.md，运行 `python -m pytest -q`、`ruff check fnmusic tests`、`node --check fnmusic/static/admin.js`、`git diff --check`；检查页面和最终差异，记录结果。

## 验收记录

- 完整回归：`.venv/bin/python -m pytest -q`，242 passed、1 skipped、1 项现有 Starlette/httpx 弃用警告。
- 沙箱内本地 Socket 被禁止；允许本地监听后完整回归通过。
- `.venv/bin/ruff check fnmusic tests`、`node --check fnmusic/static/admin.js`、`git diff --check` 均通过。
- 独立只读审查发现 GUID-only 原生播放历史分类缺口，补回归并修复；复核无新增阻塞项。另覆盖旧日志最新风格保留。
- 浏览器使用独立临时数据验证探险设置保存、雷达分组和搜索空状态，控制台无错误；示例截图保存在 `/tmp/fnmusic-version-radar.jpg`。
- 已按用户后续要求移除「猜你喜欢」入口、旧生成逻辑和失效测试，保留共用的风格别名与歌曲身份工具。
- 未打包、部署或推送。真实 NAS、官方 Web/App 和用户自有音源的兼容性留待实机验证。
