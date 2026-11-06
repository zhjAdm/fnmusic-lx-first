# GitHub Actions Node24迁移

用户报告运行42143438199的Node20弃用提示。已核对该次build和release均成功，v0.1.9已正式发布；告警来自Actions自身运行时，不是软件包的Node22。

按官方最新稳定release取实际提交并逐个读取action.yml确认runs.using=node24：checkout v7.0.1、setup-python v7.1.0、setup-node v7.1.0、upload-artifact v7.0.2、download-artifact v8.0.2。核对官方README兼容说明及全部已用inputs；保留ZIP产物格式archive:true，显式关闭未使用的Node包管理器缓存。

两份工作流所有Action固定SHA已更新；YAML解析和差异检查通过。同步README中0.1.9的真实发布状态。此变更仅CI和文档，没有修改软件运行时、没有触发新的构建或发布；下一次用户手动运行时验证新Actions，历史运行的告警不修改。
