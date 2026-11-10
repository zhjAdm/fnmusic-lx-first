# 本地开发与源码部署

普通用户建议通过 [README 的 FPK 安装流程](../README.md#安装) 使用扩展。本页面向需要调试接口或自行部署服务的开发者。

## 音乐发现模块

- `fnmusic/history.py`：`playback.db` 按脱敏用户和歌曲保存聚合播放活动；首次读写在同一写事务内导入最近保留的 500 条历史事件中的有效播放。与日志轮转分离，不保存会话凭证或媒体地址。
- `fnmusic/discovery.py`：`discovery.json` 保存全局 `exploration_mode`（`comfort` / `balanced` / `bold`，默认 `balanced`）。探险按风格及显式相邻风格规则选歌；考古先从共享索引选候选，再调用当前用户的 `Official.find_local` 校验路径，保留官方对象。
- `fnmusic/versions.py`：只读当前配置目录索引，通过保守版本后缀规则分组，普通括号内容保留；不修改去重下载所用的 `same_recording` 判定。
- `Playlists` 的 `explore` / `archaeology` 各对应一个用户专属歌单 GUID。探险缓存 15 分钟，考古缓存 5 分钟并包含用户、会话指纹、北京时间日期和索引扫描版本；播放活动触发当前用户缓存失效。管理进程与代理进程通过设置文件共享探险档位。
- 本地播放不额外请求身份接口；复用最近 5 分钟经过官方接口确认的会话归属。未有曲目信息时保留官方 GUID，考古解析官方对象后按 GUID 再核对历史分类。

管理接口沿用管理员和写请求来源校验：`GET/POST /api/discovery` 读取或保存探险档位，`GET /api/library/versions?q=&page=1&size=25` 查询版本雷达。也支持现有 `/app/fnmusic-lx-first` 前缀；雷达每页最多 100 组。

## 开发环境

需要 Python 3.11+。LX 服务需要 Node.js 20+；音频校验和部分 App 转码需要 FFmpeg / ffprobe。源码部署还需要正常运行的官方飞牛音乐及可访问的 Unix Socket。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e '.[test]'
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，保留其私密性。配置值按数据解析，不执行 shell 表达式。

## 主要配置

| 配置项 | 说明 |
| --- | --- |
| `LX_PASSWORD` | 专用 LX 实例的凭证，替换示例值，至少 16 个字符 |
| `LX_URL` | LX 服务地址，默认本机 `http://127.0.0.1:8005` |
| `LX_PLATFORMS` | 搜索平台顺序，默认 `kg,tx,kw,wy,mg` |
| `DATA_DIR` | 扩展私有数据目录 |
| `DOWNLOAD_DIR` | NAS 下载目录，需允许音乐服务读取；外部整理场景应与正式曲库分开 |
| `LISTEN_SOCKET` | 扩展代理的 Unix Socket |
| `UPSTREAM_SOCKET` | 官方音乐原 Socket；接管后指向备份路径，避免代理循环 |
| `PLAY_MODE` | `direct`：在线直连优先；`proxy`：NAS 中转。本地匹配优先于在线资源 |
| `PLAY_QUALITY` | 在线播放的音质起点，默认 `320k`，按源声明尝试可用音质 |
| `DEFAULT_PLAYLIST_GUID` | 指定当前用户可访问的已有歌单；未指定时查找或创建「默认歌单」 |
| `HOT_SOURCE` / `HOT_BOARD` | 热门推荐平台及榜单；默认 `kg,tx` 混合去重；平台以逗号分隔，榜单为空时各取首个榜单；指定榜单时建议使用单个平台 |
| `PUBLIC_PLAYLISTS` | 公共在线歌单配置，JSON 数组 |

公共歌单示例，ID 需替换为目标平台的真实歌单 ID：

```dotenv
PUBLIC_PLAYLISTS='[{"source":"kw","id":"真实歌单ID","name":"在线歌单"}]'
```

本地扫描目录通过管理页配置。旧的 `SCAN_PATH` / `SCAN_BODY` 不再触发官方扫描，飞牛音乐自行管理入库。

## 准备 LX 运行环境

本项目固定使用 lxserver 运行包，并通过脚本校验下载内容。不要把开发电脑的 `.venv`、`work` 或已下载的其他架构运行目录直接复制到 NAS。

```bash
.venv/bin/python -m scripts.runtime prepare
.venv/bin/python -m scripts.runtime native
.venv/bin/python -m scripts.runtime configure
```

若 `DATA_DIR` 不使用项目内的 `data`，对应调整 `prepare/native` 的 `--dir` 和 `configure` 的 `--path`。参数以各子命令的 `--help` 为准。

LX 应使用扩展专用实例。导入脚本会执行脚本初始化，可能产生脚本自身的网络请求；不要同时启用多个来源不明的脚本。私有脚本不提交至 Git 或构建产物。

## 在 fnOS 上部署服务

`deploy/` 中的服务模板假设源码位于 `/opt/fnmusic-lx`、数据位于 `/opt/fnmusic-lx/data`。按实际路径调整服务文件和 `.env` 后，先启动 LX：

```bash
sudo cp deploy/fnmusic-lxserver.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start fnmusic-lxserver
.venv/bin/python -m scripts.runtime import /真实路径/你的LX音源.js
```

确认音源就绪，再安装扩展代理服务。**此服务模板的启动动作会接管官方音乐 Socket，停止时恢复官方直连。**

```bash
sudo cp deploy/fnmusic-lx.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start fnmusic-lx
```

验证后可按需启用开机启动：

```bash
sudo systemctl enable fnmusic-lxserver fnmusic-lx
```

FPK 包含独立管理服务；直接使用上述源码服务模板不等同于完成 FPK 安装。

## 不接管的调试启动

```bash
.venv/bin/python -m fnmusic --env .env
```

此命令只启动代理监听器，不接管官方 Socket。不要与 systemd 扩展代理同时运行；同一个状态数据库只运行一个代理进程，不使用多个 uvicorn worker。

首次接管前可将 `UPSTREAM_SOCKET` 指向当前官方 Socket 做只读调试；接管时须改回备份路径。手动接管与恢复命令：

```bash
sudo .venv/bin/python -m scripts.socket_control enable --state /opt/fnmusic-lx/data/takeover.json
sudo .venv/bin/python -m scripts.socket_control restore --state /opt/fnmusic-lx/data/takeover.json
```

不要让多个扩展同时接管同一个官方 Socket。官方音乐升级后，应重新检查原 Socket、认证接口和播放路径。

## 检查下载任务

在已登录的官方音乐网页开发者工具中执行：

```javascript
fetch('/music/api/v1/_ext/jobs', {credentials: 'include'})
  .then(response => response.json())
  .then(console.log);
```

常见状态：

| 状态 | 含义 |
| --- | --- |
| `pending` / `downloading` | 排队或正在下载 |
| `waiting_index` / `waiting_tools` | 等待本地索引或音频组件 |
| `waiting_scan` | 等待外部整理或飞牛曲库收录，具体原因查看任务提示和日志 |
| `binding` / `done` | 正在关联歌单或已完成 |
| `waiting_auth` | 等待用户重新登录并访问 |
| `retry` / `failed` | 失败退避重试；达到 8 次后等待手动重试 |
| `cancelled` | 已取消喜欢，停止继续处理 |

会话凭证仅在内存保留，默认一小时，访问时续期。服务重启后需用户再次访问，才会恢复需要该用户凭证的任务。取消喜欢不删除已下载音乐或已加入歌单的条目。

重试失败任务：

```javascript
fetch('/music/api/v1/_ext/jobs/retry', {
  method: 'POST',
  credentials: 'include',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({guid: '在线歌曲GUID'})
}).then(response => response.json()).then(console.log);
```

## 测试与构建

```bash
.venv/bin/python -m pytest -q
```

部分测试会创建本地 Unix Socket；受限环境禁止套接字绑定时，需要在允许的环境运行。私有音源脚本测试为可选项：

```bash
PRIVATE_LX_SCRIPT='/你的路径/音源.js' .venv/bin/python -m pytest tests/test_private_script.py -q
```

私有脚本测试模拟请求回调，不等同于真实音源播放验证。实机测试应覆盖 Web / App、用户权限、不同网络下的播放、下载整理、官方入库、默认歌单与升级后状态。

双架构 FPK 构建和发布流程见 [GitHub 构建说明](github-build.md)。构建不是日常测试的前置步骤。

## 运行结构

```text
官方 Web / App → 官方 nginx → 扩展 Unix Socket
                               ├─ 官方业务 → 官方音乐原 Socket
                               ├─ 搜索 / 歌单 / 取链 → 专用 LX 服务 → 用户音源脚本
                               ├─ 播放 → 本地曲库 / 在线直连 / 内存中转
                               └─ 喜欢 → 下载 → 可选外部整理 → 官方入库 → 默认歌单
```

扩展只使用自己的 SQLite 数据库，不直接修改官方音乐数据库。更多运行包和协议参考见 [第三方说明](../THIRD_PARTY_NOTICES.md)。
