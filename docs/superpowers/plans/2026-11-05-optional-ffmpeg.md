# FFmpeg 按需使用 Implementation Plan

**Goal:** FPK 不内置 FFmpeg；复用已有程序并支持管理页按需下载。
**Architecture:** fnmusic/tools.py 负责发现、探测与经过校验的异步组件安装；管理页轮询状态；运行模块按需解析路径。
**Tech Stack:** Python / asyncio / HTTPX / FastAPI / 原生 HTML。

- [x] 1. tests/test_tools.py 编写发现、缺失、下载校验与安全提取测试；运行确认失败。
- [x] 2. fnmusic/tools.py 实现解析与组件安装；固定下载清单放 fnmusic/ffmpeg-assets.json。
- [x] 3. audio/transcode/jobs 接入缺失等待；manager/control/static 增加状态及主动下载入口；验证权限、并发与生命周期清理。
- [x] 4. build_fpk skeleton 清理旧 runtime/ffmpeg，停止打包；版本0.1.2，更新 README/许可与安装说明。
- [x] 5. 全量测试、lint、真实 Linux 两架构组件检查、fnpack 构建、归档源码与 SHA 核对；交付 FPK。

每项先观察行为测试失败再实现，全部测试通过后提交本地分支，不连接 NAS。
