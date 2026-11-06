# 0.1.14 修复升级等待

**目标：** 修复实际设备上下载完成但安装版本不变、页面一直等待新版服务的问题。

## 已验证根因

用户在 fnOS 1.2.1 重复执行原生 install-fpk，得到退出码0及 `[Info]Application [fnmusic-lx-first] is installed.`，磁盘源码和运行API均仍为0.1.12。原方案依据帮助中的升级字样判定覆盖升级可行，证据不足；退出码0并不证明发生升级。

同机只读探测 `POST /rpc/v1/update/info {}`，经 `/var/run/com.trim.app.center.sock` 返回 `{code:10030,msg:""}`，确认存在参数校验的升级前检查接口。

## 实现及验收项

- [x] 独立Python标准库客户端实现本地HTTP/Unix Socket协议；只允许暂存、升级前检查、升级任务和状态查询路由。
- [x] 复现HTTP200业务错误、任务失败、版本未变、身份不符和提交结果未知；不把这些情况标为成功。
- [x] 核对目标应用、版本、原安装卷及数据卷；不调用卸载、安装或install-local作为替代。
- [x] 提交只执行一次，超时、丢失任务ID或提交后的本地写入失败保持结果未知，禁止自动重提。
- [x] 私有安装副本、独立Python运行环境和worker在停止旧应用前完成暂存，独立systemd服务继续执行。
- [x] 根据任务成功、磁盘版本和新管理服务运行版本完成确认；新版本可接管旧版遗留的等待状态。
- [x] 本地完整回归186项通过、1项既有跳过、1条既有测试工具弃用警告；Ruff、JS语法及差异检查通过。独立审查的提交后写入失败及失联超时分类问题均已补回归修正，复审无剩余可操作问题。
- [ ] NAS实际升级验证：当前仅确认旧CLI失败及本地守护进程接口存在，不能将本地协议测试当成实机升级成功。

## 协议事实来源

研究了实现者公开记录的接口及响应字段，自行实现Python客户端，没有引入其Go实现或依赖：

- https://github.com/conversun/fnos-store/commit/91de88ffabe2ed4c1ac28a96e525242bd0b0f9f1
- https://github.com/conversun/fnos-apps/issues/189

协议路径：download/task → download/status → update/info → update/task → common/status。状态1为执行中、2为完成、5为任务不可观测；HTTP200仍须检查业务code。升级使用原存储卷，服务恢复交由守护进程；不额外调用CLI重复启动。

本地已构建x86/ARM64的0.1.14修复FPK；双包通过scripts.verify_fpk（源码一致、私密输入排除、ELF架构、SHA256）和自动升级包身份/版本/架构/包内MD5校验。完整修复后的NAS升级仍未实际执行。
