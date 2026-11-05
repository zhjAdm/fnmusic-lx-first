# FPK 0.1.3 精简版验证

- 用户提供的 NAS 日志明确显示：带双引号的 WorkingDirectory 被 systemd 判为非绝对路径，三个服务均不能启动。修复为不带外围引号的绝对路径，仅对 systemd 的 % 说明符转义。
- 真实 Debian systemd-analyze 复现原错误；修复后的音源、管理、代理三个服务文件全部通过 verify（不需要特权容器）。
- 69 项自动化测试通过，1 项 Starlette 测试工具弃用警告；Ruff / JavaScript 语法检查通过。新增 WorkingDirectory 与私有监听补丁回归测试。
- 修复 LX 固定版本忽略 BIND_IP、实际硬编码0.0.0.0的行为。构建和 prepare 均强制127.0.0.1，重复应用安全。
- 两架构在无网络 Linux 容器中实际启动打包的 Node 音源与 Python 管理服务；认证 API、管理员网关拒绝、Unix Socket 可连接；/proc/net/tcp 确认音源仅在127.0.0.1监听。
- 官方 fnpack 1.2.3 构建两架构包，归档校验和、当前源码字节、Python/Node ELF 架构通过，不含 FFmpeg、私密音源或用户配置。
- 未连接用户 NAS，修复依据为用户日志与隔离复现；真实应用中心升级及官方 App 验收待进行。

| 架构 | 字节数 | 压缩体积 | SHA256 |
|---|---:|---:|---|
| x86 | 109039702 | 104.0 MiB | `23df610cdd80980cfb2341fae81ab6e980e1d6aecd7ed7b428a583e66d3bd4db` |
| arm | 104034374 | 99.2 MiB | `8c1a63d40b6d2dfa0627ca0d8faf1875ed800427a35354bd6a408291a6153fcc` |
