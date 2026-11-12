# CodePilot v0.1.0-beta.1

发布日期：2026-11-12。此版本用于代码展示与受控环境验证，标记为 GitHub 预发布版本。

本次从多项目工作区抽取 CodePilot、shared 和离线评估依赖 AgentOps，保留原目录边界。Knowledge Agent、模型密钥、运行数据和原工作区 Git 元数据不包含在发布内容中。

## 功能

- 搜索和读取代码、提出 Diff、人工审批、应用补丁、pytest 验证。
- API Key / RBAC、任务接口、请求幂等、审计、尝试链和运行指标。
- SQLite 审批检查点及明确暂停节点的恢复。
- 本地线程池和 Redis Streams Worker 两种初始任务执行方式。
- 五条确定性评估、单元/API 测试和真实 Redis 集成测试入口。

## 已知限制

执行隔离、同仓库并发保护、Worker 执行租约及故障恢复、跨进程取消尚未完善。审批后续执行仍由 API 推进。不包含公网部署服务，不宣称真实模型成功率。历史验证证据在 ACCEPTANCE.md，本次独立发布的验证见下文。

## 本次验证

在独立发布目录新建虚拟环境并从 pyproject.toml 安装依赖后，2026-11-12 本机验证：95 passed、1 skipped。跳过项是未提供专用 Redis 的真实集成测试。Ruff、前端 JavaScript 语法、离线 Demo 和五条确定性评估通过。测试出现一条第三方 Starlette/AnyIO 弃用警告。

本轮本机 Docker 引擎无法连接，因此在 GitHub Actions 中完成真实 Redis 和容器验证：[运行记录](https://github.com/generga/CodePilot/actions/runs/43216024272)。Linux Python 3.12 下 96 项测试通过，无跳过；Ruff、前端语法、镜像构建以及只读根文件系统容器内五条确定性评估全部通过。验证不包含 DeepSeek 真实调用、完整 Compose 多进程验收或生产负载测试。

## 复验

从仓库根目录安装 .[dev,worker]，按 README 设置 PYTHONPATH，执行 pytest、Ruff、node --check 和 codepilot.evaluation。真实 Redis 集成需专用 Redis 地址，否则会跳过。
