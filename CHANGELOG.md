# Changelog

## v0.1.0-beta.1 - 2026-11-12

首次独立预发布，包含代码检索、补丁审批与测试闭环，FastAPI 控制台、SQLite 持久化、Redis Worker、运行指标和确定性评估。

修复 API 状态缓存、新消费者启动误判和消息确认缺陷。随附共享契约与 AgentOps 评估模块。

当前不宣称生产就绪，不包含公网托管服务。执行隔离、租约恢复及跨进程取消待完善，见 docs/ACCEPTANCE.md。
