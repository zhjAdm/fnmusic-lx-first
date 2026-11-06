# 0.1.16 NAS 本地安装包类型修复

用户NAS实测：download/task使用`packageSourceType=file`暂存私有FPK，download/status返回status=2、正确应用和目标版本、installed=true，但`packageType=path`。旧代码将packageType硬编码为file，导致staged_package_mismatch，未生成submitted/task-id，没有执行升级。

同机update/info使用file返回10100；改用path返回code=0、正确目标版本0.1.15及installedVolumeID=3。确认来源类型与解析后的包类型不是同一字段。

修正：只接受file/path两种本地包类型，保留应用名、版本、installed及安装/数据卷核对；info和task均传递暂存接口实际返回的packageType。其他类型仍拒绝，不回退到卸载重装。

新增回归：path包通过并在info/task中原样传递；cloud、空值、缺失和错误类型不能提交升级。修复前path回归失败，修复后通过。

用户明确不生成安装包。本次仅修复代码、更新版本到0.1.16和提供可回退的两行NAS热修方法。热修只修改旧版worker.py，备份原文件并校验/原子替换，不主动提交升级；用户可随后主动点击下载并升级验证0.1.15。0.1.15旧安装包会覆盖临时热修，后续正式版本应包含本次修复。

本地验证：完整回归191 passed、1 skipped及1条既有测试工具弃用警告，Ruff、JS语法、差异检查通过。热修命令在旧版源码副本验证，备份与原始内容一致，替换后与本次修复源码一致。尚待用户返回NAS真实升级结果；没有生成安装包。

用户NAS热修结果：终端已返回“已修复”，原worker文件备份成功；尚待网页重新触发后的真实版本/任务结果，不能据此声称自动升级成功。
