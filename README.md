# 飞牛音乐 LX 扩展

保留官方飞牛音乐 Web / App，接入用户自有的 LX 自定义音源。当前为 **0.1.6 开发版**，已完成本机自动化验证，尚未在真实 fnOS 和官方手机 App 上验收。

## 推荐：直接安装 FPK

从 `dist/` 选择 `fnmusic-lx-first-0.1.6-x86.fpk`（Intel/AMD）或 `fnmusic-lx-first-0.1.6-arm.fpk`（ARM64），在飞牛应用中心手动安装。软件包自带 Linux Python、Node 和 LX 服务；FFmpeg / ffprobe 优先复用 NAS 已有兼容版本，缺少时由管理员在管理页按需下载，不需要手动部署源码或在 NAS 上安装 Python 依赖。安装和普通播放不会自动下载音频组件。需已安装官方飞牛音乐。

安装后打开桌面「fnMusic 扩展管理」：

1. 上传自己的 LX `.js` 音源脚本。
2. 填写属于官方曲库扫描范围的下载目录并保存。
3. 查看「音频组件」状态。缺少时点击按需下载；从固定版本的 GitHub 发布包下载并校验 SHA256，仅保存到扩展数据目录，不改动系统组件。
4. 按需打开「接管官方音乐」。默认关闭；关闭时恢复官方直连，管理界面继续运行。

停止应用会恢复官方直连；重新启动会恢复之前已保存的接管偏好。卸载不删除下载目录内的音乐，配置会在存储卷根目录保存私密备份。FPK 是未完成真实 NAS 验收的开发版；管理入口通过飞牛管理员桌面网关访问，不开放独立管理端口。

## 本地曲库扫描与下载去重

管理页可设置多个 NAS 音乐目录、开始/取消扫描并查看检查、索引、跳过和异常数量。直接读取音频标签，以 Mutagen 和扩展自己的 SQLite 数据库建立索引；不调用飞牛扫描接口，不需要曲库 GUID 或官方扫描凭证。扫描不修改、移动或删除音乐文件，也不跟随符号链接。

开启「下载前自动更新本地索引」后，任务会先更新索引（60秒内完成的结果会复用），再检查标题、歌手、专辑、时长及实际文件状态。文件已删除或修改、信息不完整或版本不一致时不会误判重复。跨音源的同一录音也可复用扩展已下载文件。取消扫描后，启用自动更新的下载任务等待重新扫描，不消耗失败重试次数；也可关闭自动更新，仅使用已有索引。

扩展的本地索引不能替代官方App入库。找到重复文件会跳过下载，但加入官方默认歌单仍要用当前用户的官方接口确认同一路径歌曲的GUID，未收录时保持等待；本扩展不自动触发官方扫描。

搜索内部调用现使用官方App的`q`参数；兼容list/items/tracks/records列表，一条坏在线记录不会拖垮整批结果。本地查询失败但在线结果可用时保留在线结果，并记录失败阶段及官方错误码。身份认证失败仍会拒绝请求。

## 运行日志

管理页「运行日志」支持日志来源、级别筛选、手动刷新、每5秒刷新和下载最近记录。只有飞牛管理员可查看和下载。

- 扩展运行日志：启动、停止、音源加载、设置操作、音频组件下载和收藏任务结果，保留异常类型、业务代码位置、HTTP状态、上游服务及安全接口路径。查询参数、凭证和远端媒体地址不进诊断摘要。
- 搜索与播放记录：记录已认证搜索的关键词、结果数、缓存命中与耗时；在线播放记录歌名、歌手、音源、音质、返回方式和失败。HEAD与后续Range请求不重复当作新播放。官方本地歌曲优先使用近期搜索中已有元数据，未取得时仅记录曲目ID及「未取得曲目信息」，不额外查询官方接口。
- 系统服务诊断：只读三个扩展服务的 systemd 启停记录，不导出音源脚本的原始控制台输出。
- 启动脚本日志：即使 Python 尚未启动，也保存失败信息，并写入应用中心的错误提示。

日志在应用私有数据目录的 `logs/` 中。扩展日志单文件1MiB、保留3份历史；启动日志另外有大小限制和1份历史。不会记录音源正文、请求 Cookie/授权头和原始异常消息，查看及下载时再次隐藏已知凭证与在线URL。发生问题时下载对应来源日志即可；日志页不是整机系统日志导出。搜索词与曲目信息也仅管理员可查看，遵循相同轮转上限；播放日志描述服务端收到请求/返回直链/开始供流，不能确认客户端实际播放或播放完成。

## 已实现的行为

- 在线搜索、播放、歌词、封面，以及配置的在线歌单、热门推荐、根据收藏歌手生成的推荐。
- 默认尝试在线原文件直连；无法返回直链时尝试 NAS 流式代理，在线失败再通过当前用户的官方 API 严格匹配本地歌曲。
- **普通播放不保存音频文件**。代理使用内存缓冲；App 的 HLS 兼容路径使用 ffmpeg 内存管道输出 AAC。
- **只有点击喜欢才创建下载任务**。先检查独立的本地目录索引；标题、歌手、专辑和时长匹配且文件存在时，跳过下载并直接收藏/加入默认歌单。扩展已下载的同一录音也可跨音源复用；信息不全或版本不同时不误判为重复。需要下载时按源实际声明的最高音质逐级尝试。校验完整长度、音轨和时长，写标签后发布到曲库目录。
- 等待官方扫描确认同一路径的本地歌曲后，通过官方 API 收藏并加入当前用户的【默认歌单】。不存在时尝试创建；可配置已有歌单 GUID。
- 收藏和任务按用户隔离、重复点击去重。凭证只在内存；任务持久保存，重启后等该用户重新访问恢复。
- 停止或取消喜欢不删除已经下载的音乐；已加入默认歌单的条目保留。

缺少兼容音频组件时，搜索、直链和原文件中转仍可使用；部分 App 的转码播放会提示缺少 FFmpeg，喜欢任务会等待 ffprobe 就绪后再下载音频，不消耗失败重试次数。按需下载依赖 NAS 能访问 GitHub，失败后可在管理页重试。已下载的组件在升级时保留。

## 播放边界

**302 发出后，手机直接连接音源，NAS 无法知道手机端是否播放失败。**目前不能保证“手机直连失败后无缝切换 NAS”。服务器能处理的是返回播放响应之前的失败。若手机经常跳歌，将 `.env` 中 `PLAY_MODE` 改成 `proxy` 并重启扩展。

代理响应开始后也不会把不同音质或本地文件拼到同一流中。断点续传绑定同一播放资源，绑定过期会要求重新播放。HLS 采用一首歌一个 AAC 分片以避免音频落盘；定位播放可能需要重新请求并从头转码，没有分段缓存方案的 seek 性能。HLS 由 NAS 供流。

搜索目前为每次查询最多 100 首在线候选，加上官方本地结果的 60 秒快照分页。公共歌单最多读取 100 页；不会把截断的列表假报为导入完成。猜你喜欢基于收藏歌手搜索，不是平台账号专属每日推荐，不使用大模型。候选歌曲能否播放取决于音源、密钥权益及歌曲资源。

## 运行结构

```text
官方 App / Web → 原官方 nginx → 扩展 Unix Socket
                                ├─ 官方业务 → 官方音乐原 Socket
                                ├─ 搜索/歌单/取链 → 私有 lxserver → 用户的 LX 脚本
                                ├─ 播放 → 直链 / 内存中转 / 官方本地歌曲
                                └─ 喜欢 → 下载队列 → 曲库扫描 → 官方默认歌单
```

新扩展独立实现，不运行 `work/upstream` 中的参考代理。搜索与脚本运行依赖 [lxserver v2.1.2](https://github.com/XCQ0607/lxserver/releases/tag/v2.1.2)。`scripts/runtime.py` 校验发行包 SHA256，并补充运行时真实音质声明；不修改脚本，也不默认授权原生不安全 VM。

## 在 fnOS 上安装源码版

需要 Python 3.11+、Node.js 20+、ffmpeg/ffprobe、正常运行的官方音乐。服务模板假设项目位于 `/opt/fnmusic-lx`，NAS 目录不同需要同步调整模板和 `.env`。

1. 将此项目的源码复制到 NAS。不要复制 `.venv`、`work` 或 Mac 的 `data/runtime`；NAS 上重新准备对应平台运行包和原生依赖。
2. 在项目目录创建虚拟环境并安装依赖：

   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.lock
   cp .env.example .env
   chmod 600 .env
   ```

3. 编辑 `.env`：

   - 将 `LX_PASSWORD` 换成至少 16 位随机值。
   - 将 `DATA_DIR` 和 `DOWNLOAD_DIR` 改成真实路径。**下载目录必须属于官方音乐扫描范围，且官方音乐服务账号可遍历目录、读取文件。**新目录会使用 0755、完整文件使用 0644，已有共享目录权限不会被更改。
   - 已有默认歌单时设置 `DEFAULT_PLAYLIST_GUID`，避免重名歌单歧义。
   - 本地目录扫描通过管理页配置。旧的 `SCAN_PATH` / `SCAN_BODY` 不再触发官方扫描；官方 App 入库由官方自行管理。
   - 公共歌单示例：`PUBLIC_PLAYLISTS='[{"source":"kw","id":"真实歌单ID","name":"在线歌单"}]'`。
   - 初次验收建议设置 `PLAY_MODE=proxy`，先验证完整链路，再切回 `direct` 验证手机直连。

4. 准备固定运行包，并按 **NAS 当前系统和架构**补齐原生依赖：

   ```bash
   .venv/bin/python -m scripts.runtime prepare
   .venv/bin/python -m scripts.runtime native
   .venv/bin/python -m scripts.runtime configure
   ```

   如果 `DATA_DIR` 不是项目内的 `data`，上面 `prepare/native` 的 `--dir` 和 `configure` 的 `--path` 也应同步指定。服务模板使用 `/opt/fnmusic-lx/data`。

5. 启动 LX 服务，再导入自己的脚本。付费脚本存放在本机私密目录，不提交到 Git、不打包分发：

   ```bash
   sudo cp deploy/fnmusic-lxserver.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl start fnmusic-lxserver
   .venv/bin/python -m scripts.runtime import /真实路径/你的LX音源.js
   ```

   导入成功会在私有 LX 数据目录保存脚本并启用。这个操作会执行脚本初始化，可能产生脚本自身的更新检查请求。重复导入同名脚本可能被拒绝；更新时先在本机 LX 管理入口管理旧源。不要同时启用其他脚本，否则最高质量能力可能属于另一脚本；第一版要求专用 LX 实例、只启用这一个音源。

6. 安装扩展服务并启动。**这个服务模板的启动动作会显式接管官方音乐 Socket**，停止服务会恢复官方直连：

   ```bash
   sudo cp deploy/fnmusic-lx.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl start fnmusic-lx
   ```

   验证后可自行启用开机启动：`sudo systemctl enable fnmusic-lxserver fnmusic-lx`。也可直接使用 FPK 安装，见本文开头。

## 不接管的开发启动

```bash
.venv/bin/python -m fnmusic --env .env
```

该命令只启动独立代理监听器，不接管。不要与 systemd 扩展服务同时运行。一个数据库只运行一个代理进程，不要配置 uvicorn 多 worker。首次接管前可把 `UPSTREAM_SOCKET` 指向当前官方 Socket 做只读调试；接管时必须改回备份路径，否则请求会循环。

手动接管与恢复：

```bash
sudo .venv/bin/python -m scripts.socket_control enable --state /opt/fnmusic-lx/data/takeover.json
sudo .venv/bin/python -m scripts.socket_control restore --state /opt/fnmusic-lx/data/takeover.json
```

接管前检查独立代理健康；官方 Socket 备份位置被占用、其他部署接管、官方升级后 Socket 改变时会保留现场并报错，不覆盖未知端点。不要与原项目或其分支同时接管同一官方 Socket。官方升级可能需要先停止扩展，完成升级后重新验收。

## 任务检查和重试

在已登录的官方音乐网页中使用开发者工具执行（JSON 只包含当前用户任务，不含 Cookie）：

```js
fetch('/music/api/v1/_ext/jobs', {credentials: 'include'}).then(r => r.json()).then(console.log)
```

状态包括 `pending`、`downloading`、`waiting_scan`、`binding`、`done`、`retry`、`waiting_auth`、`failed`、`cancelled`。登录会话仅在内存保留一小时，持续访问续期；失效后需重新登录访问。失败采用退避，8 次后等待手动重试。扫描请求被中断时可恢复重试，等待扫描期间最多每五分钟再请求一次。

```js
fetch('/music/api/v1/_ext/jobs/retry', {
  method: 'POST', credentials: 'include',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({guid: '在线歌曲GUID'})
}).then(r => r.json()).then(console.log)
```

如果下载完成一直等待扫描，先检查目录是否在官方扫描范围、目录 ACL 是否允许读取，再从官方音乐触发扫描。若扫描后仍不绑定，需要核对当前版本搜索结果 `audioSpec.path` 与 NAS 真实路径以及歌单接口。

## 验证

```bash
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check fnmusic scripts tests
.venv/bin/ruff format --check fnmusic scripts tests
```

真实 LX 接口测试使用人工脚本；私密源检查需另外指定本地路径，只模拟请求回调，不发真实音源网络请求：

```bash
PRIVATE_LX_SCRIPT='/你的路径/音源.js' .venv/bin/python -m pytest tests/test_private_script.py -q
```

真实 NAS 验收需核对：官方版本与认证方式、网页和 App 搜索/播放/歌词/封面、手机 Wi-Fi/蜂窝直连、HLS AAC 兼容性、收藏下载、扫描入库、默认歌单和多用户隔离，以及服务停止后的官方本地播放。**本机通过不代表以上真实设备验收已完成。**当前官方接口适配参考原项目针对飞牛音乐 1.0.10 的实现，创建歌单及扫描配置尤其需要实机核对。

付费源脚本、运行目录和 `.env` 均排除 Git；LX 自身的脚本日志可能包含敏感内容，分享日志前需在本机检查。音源执行由独立进程承担，VM 不是完整安全隔离；只导入自己信任的脚本。

## 超时诊断（0.1.6）

0.1.5用户日志中的ReadTimeout尚不能仅凭栈确认具体接口。用户的只读检查显示官方原始Socket和LX均快速返回401、接管指向正常。0.1.6为官方/LX/媒体/封面请求加入仅本地使用的服务标记，异常日志自动记录服务、方法、安全路径与业务帧；聚合搜索和播放降级也保留失败阶段。未凭推测扩大超时或宣称原故障已修复。重现问题后下载「扩展运行日志」，可直接看到超时请求的接口类别。

管理页按功能拆分为「扩展设置」「本地曲库」「音频组件」「搜索与播放」「运行日志」五个菜单；切换菜单保留未保存的设置，扫描和组件下载继续在后台进行。历史记录与诊断日志分别筛选、下载。

0.1.7：等待官方收录时，同一文件的跳过下载和等待状态只记录一次，跨服务重启保留判断；若文件不可复用而重新下载，保留真实下载日志并新增无路径的诊断警告。此版本不修复已报告的音源HTTP失败或官方HLS超时。
