# 使用 GitHub 构建 FPK

工作流：`.github/workflows/build-fpk.yml`，名称 **Build fnOS FPK**。

该工作流只接受手动 `workflow_dispatch`；普通提交、推送或标签不会自动构建，也不会自动创建 Release。无需添加音源密钥或个人访问令牌到 Actions Secrets。

## 手动构建

1. 将代码和工作流推送到自己的 GitHub 仓库。工作流需存在于仓库默认分支，才会显示手动运行入口。
2. 进入 **Actions → Build fnOS FPK → Run workflow**，选择要构建的分支，点击运行。
3. 等待安装依赖、测试、双架构构建和包校验通过。
4. 打开此次运行页面，在 **Artifacts** 下载 `fnmusic-fpk-运行编号`。解压后得到 x86、arm 两份 FPK 和对应 SHA256 文件。
5. Intel/AMD NAS 选择 x86；ARM64 NAS 选择 arm，按常规方式在飞牛应用中心手动升级。

产物保留30天，需要长期保存时自行下载。此工作流仅生成构建产物，不上传付费音源、不连接NAS，不会自动安装或开启官方接管。

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

修改版本时同步更新 `pyproject.toml`、`packaging/fpk/manifest.in`、`scripts/build_fpk.py` 及管理页页脚/资源版本参数。

官方参考：[手动触发工作流](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)、[下载构建产物](https://docs.github.com/en/actions/managing-workflow-runs-and-deployments/managing-workflow-runs/downloading-workflow-artifacts)、[upload-artifact](https://github.com/actions/upload-artifact)。
