# 飞牛音乐 LX 扩展

保留官方飞牛音乐 Web / App，接入用户自有的 LX 自定义音源。当前为 **0.1.2 开发版**，已完成本机自动化验证，尚未在真实 fnOS 和官方手机 App 上验收。

## 推荐：直接安装 FPK

从 `dist/` 选择 `fnmusic-lx-first-0.1.2-x86.fpk`（Intel/AMD）或 `fnmusic-lx-first-0.1.2-arm.fpk`（ARM64），在飞牛应用中心手动安装。软件包自带 Linux Python、Node 和 LX 服务；FFmpeg / ffprobe 优先复用 NAS 已有兼容版本，缺少时由管理员在管理页按需下载，不需要手动部署源码或在 NAS 上安装 Python 依赖。安装和普通播放不会自动下载音频组件。需已安装官方飞牛音乐。

安装后打开桌面「fnMusic 扩展管理」：

1. 上传自己的 LX `.js` 音源脚本。
2. 填写属于官方曲库扫描范围的下载目录并保存。
3. 查看「音频组件」状态。缺少时点击按需下载；从固定版本的 GitHub 发布包下载并校验 SHA256，仅保存到扩展数据目录，不改动系统组件。
4. 按需打开「接管官方音乐」。默认关闭；关闭时恢复官方直连，管理界面继续运行。

停止应用会恢复官方直连；重新启动会恢复之前已保存的接管偏好。卸载不删除下载目录内的音乐，配置会在存储卷根目录保存私密备份。FPK 是未完成真实 NAS 验收的开发版；管理入口通过飞牛管理员桌面网关访问，不开放独立管理端口。

## 已实现的行为

- 在线搜索、播放、歌词、封面，以及配置的在线歌单、热门推荐、根据收藏歌手生成的推荐。
- 默认尝试在线原文件直连；无法返回直链时尝试 NAS 流式代理，在线失败再通过当前用户的官方 API 严格匹配本地歌曲。
- **普通播放不保存音频文件**。代理使用内存缓冲；App 的 HLS 兼容路径使用 ffmpeg 内存管道输出 AAC。
- **只有点击喜欢才创建下载任务**，按源实际声明的最高音质逐级尝试。校验完整长度、音轨和时长，写标签后发布到曲库目录。
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
   - 有当前官方版本的实际扫描请求时填写 `SCAN_PATH` 和 `SCAN_BODY`；为空时等待官方自动扫描或手动扫描。扩展不会猜测扫描接口。
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
