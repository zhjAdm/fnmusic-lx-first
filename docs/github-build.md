# 使用 GitHub 构建 FPK

工作流：`.github/workflows/build-fpk.yml`，名称 **Build fnOS FPK**。

该工作流只接受手动 `workflow_dispatch`；普通提交、推送或标签不会自动构建，手动构建时可勾选发布到 Release；普通push或tag不触发。无需添加音源密钥或个人访问令牌到 Actions Secrets。

## 手动构建

1. 将代码和工作流推送到自己的 GitHub 仓库。工作流需存在于仓库默认分支，才会显示手动运行入口。
2. 进入 **Actions → Build fnOS FPK → Run workflow**，选择要构建的分支，点击运行。
3. 等待安装依赖、测试、双架构构建和包校验通过。
4. 若勾选发布，构建成功后会在Releases发布版本、建立版本标签并附加FPK和SHA256；已发布版本拒绝覆盖，需先更新源码版本。
5. 打开此次运行页面，在 **Artifacts** 下载 `fnmusic-fpk-运行编号`。解压后得到 x86、arm 两份 FPK 和对应 SHA256 文件。
6. Intel/AMD NAS 选择 x86；ARM64 NAS 选择 arm，按常规方式在飞牛应用中心手动升级。

产物保留30天，需要长期保存时自行下载。工作流不上传付费音源、不连接NAS，不会自动安装或开启官方接管。

## 构建内容与校验

Ubuntu x86 runner 交叉准备两个目标架构的 Python、Node、Python wheels 和 LX 运行时，再调用官方 Linux fnpack 1.2.3。公开归档由 `assets.lock.json`、`build-tools.lock.json` 和 `scripts/runtime.py` 中的校验和固定。

`prepare_fpk.py` 可从空暂存目录准备公共依赖。`verify_fpk.py` 校验包SHA256、manifest载荷校验和、版本、所有业务/静态源码和Python/Node ELF架构，并拒绝付费音源、`.env` 和内置FFmpeg/ffprobe。外部Python wheels固定版本，LX原生npm模块按已有实现验证registry完整性。

运行单元测试时只使用模拟音源；没有私有脚本的离线契约测试会明确跳过，真实固定版本LX运行时测试仍会执行。云端构建成功不表示真实音源、官方App或NAS升级已验收。

本机Linux复现：

```sh
python -m pip install -r requirements.lock
python -m pip install -e '.[test]'
python -m scripts.runtime prepare
python -m scripts.runtime native
python -m pytest -q
python -m scripts.prepare_fpk --arch both
python -m scripts.build_fpk --arch both --fnpack work/fpk/cache/fnpack-1.2.3-linux-amd64
python -m scripts.verify_fpk dist/*.fpk
```

修改版本只需更新 `fnmusic/__init__.py` 的 `__version__`，使用 `主版本.次版本.修订号` 格式。Python 包元数据和 FPK 构建脚本统一读取它，FPK 清单的 `@VERSION@` 在构建时替换，管理页页脚、页面版本属性和 CSS/JS 缓存参数在响应时替换；源码模板中保留占位符，不需要手动同步其他文件。第三方运行时版本和历史记录不随应用版本修改。

版本回归检查：`python -m pytest tests/test_updates.py::test_release_versions_are_numeric_and_match_package_metadata tests/test_fpk_builder.py tests/test_manager.py::test_page_versions_follow_running_version_without_editing_template -q`。测试会使用模拟的新版本检查清单、桌面入口、页面页脚及资源参数，并确认 FPK 包内源码保持原样，兼容现有源码完整性校验。

官方参考：[手动触发工作流](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)、[下载构建产物](https://docs.github.com/en/actions/managing-workflow-runs-and-deployments/managing-workflow-runs/downloading-workflow-artifacts)、[upload-artifact](https://github.com/actions/upload-artifact)。

## 发布已有构建

对已完成且仍有Artifacts的构建，可运行 **Publish existing FPK**，填入原Build fnOS FPK的运行ID。它从GitHub服务器直接下载已有产物，用对应源码提交验证后发布，不重新构建，不覆盖已发布版本；仅可补全同一提交的未发布草稿。

源码仓库已按用户要求公开：https://github.com/zhjAdm/fnmusic-lx-first 。安装包入口是[Releases](https://github.com/zhjAdm/fnmusic-lx-first/releases)，Tags展示关联的源码版本标签；FPK位于Release的Assets中。公开Release可供NAS匿名检查下载，不需要将个人GitHub凭据存进软件。

0.1.9源码增加管理页软件更新入口；下载并校验后仍由用户到应用中心手动升级。0.1.9已由用户手动构建并发布，安装后可使用软件内更新入口。

工作流依赖的官方Actions已升级到使用Node24的稳定版本，并固定提交SHA。`setup-node`中的`node-version: 22`是LX测试所用运行时；安装包Node版本由assets.lock.json固定，与Actions宿主的Node24是不同层级。普通推送仍不会触发打包。
