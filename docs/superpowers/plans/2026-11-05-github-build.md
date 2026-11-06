# GitHub手动构建准备

用户希望推送代码并在GitHub构建，已有偏好是不自动发包。采用仅workflow_dispatch触发的工作流，普通push和tag不会构建或发布。

已实现：

- 固定官方Actions提交，Ubuntu24.04/Python3.13/Node22；只读contents权限，不持久化checkout凭证。
- 公共构建依赖准备脚本：固定SHA256的Python/Node/LX/Linux fnpack，跨架构下载固定版本wheels，原子下载校验失败不覆盖缓存。
- 两架构打包后核对SHA256、manifest、当前源码、ELF架构、付费音源/本地配置排除。
- 仅上传FPK和SHA256，保留30天；不需要私有音源或密钥，不自动创建Release。
- 构建说明和README当前版本修正。

验证：新增6项用例通过；完整离线回归131项通过，1项既有Starlette警告。YAML解析、Ruff和差异检查通过。Linux容器中官方fnpack从新暂存目录完整构建两架构，校验通过，内置x86 Python业务依赖可加载。公开归档复用本地已验证缓存，Python wheels通过准备脚本取得。独立审查未发现阻断问题。Git历史文件名检查未发现私有音源或.env。

尚未推送和在GitHub实际运行：本地没有remote，未安装gh。已向用户询问目标仓库URL，或新仓库用户名/名称/可见性；收到目标后继续配置远程、核实权限并推送，再验证首次云端构建。不得默认推送参考项目javycoder/fnos_music_ext。
