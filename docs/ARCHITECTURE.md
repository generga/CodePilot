# CodePilot 架构说明

## 1. 系统定位

CodePilot 是一个带人工审批门槛的研发协作 Agent。它负责在受控仓库内定位问题、生成统一补丁、等待 reviewer 审批、执行白名单测试，并把任务状态、审计事件和运行指标持久化。

它不直接合并代码，也不允许模型执行任意 Shell 命令。高风险写操作始终经过审批节点。

## 2. 部署拓扑

```text
Browser / API Client
          |
          v
   FastAPI API Container
   - API Key + RBAC
   - repo root allowlist
   - idempotency
   - task query / approval
          |
          +---------------------> SQLite volume
          |                       - tasks
          |                       - approval_sessions
          |                       - task_events
          |                       - task_metrics
          |
          +---------------------> Redis Streams
                                  - consumer group
                                  - pending reclaim
                                      |
                                      v
                              Worker Container(s)
                              - atomic SQLite claim
                              - model client
                              - CodePilot Agent
                              - safe tools
                                      |
                                      v
                                Mounted repo
```

本地默认使用 `ThreadPoolExecutor`。设置 `CODEPILOT_EXECUTION_MODE=redis` 可将初始任务交给独立 Worker；当前审批后续执行仍由 API 推进。此部署方式仍需完善租约恢复与跨进程取消，不能据此认定生产就绪。

## 3. 一次任务的数据流

1. API 校验身份、仓库路径和幂等键，写入 `pending` 任务。
2. Redis 模式下 API 只投递任务 ID，不创建模型客户端。
3. Worker 消费消息，通过 SQLite 条件更新原子认领 `pending` 任务。
4. Agent 使用 `search_code`、`read_file`、`get_git_diff` 等只读工具理解仓库。
5. Agent 通过 `propose_patch` 生成统一 Diff，状态进入 `waiting_approval`。
6. reviewer 批准后才允许 `apply_patch` 修改仓库，随后执行白名单测试。
7. 任务结果、生命周期事件、模型 Token 和延迟指标写回 SQLite。
8. 控制台通过任务、尝试链、事件和指标 API 查询结果。

## 4. 关键一致性设计

| 风险 | 控制措施 |
| --- | --- |
| HTTP 重试造成重复任务 | SQLite 幂等键保存请求指纹和响应 |
| Redis 重复投递 | Consumer Group + SQLite `pending -> running` 条件更新 |
| Worker 崩溃后消息悬挂 | Redis 支持 pending reclaim，但业务任务安全恢复尚未完成 |
| 审批前修改代码 | `propose_patch` 只保存 Diff，不写仓库 |
| 模型越权读写 | 路径白名单、声明路径校验、工具参数校验 |
| 任意命令执行 | `run_tests` 只允许固定命令结构和超时 |
| 服务重启丢失审批 | SQLite 持久化 continuation session |
| 自动重试重复修改代码 | 自动重试仅覆盖 Worker 初始化模型失败，Agent 执行失败需人工触发 |

## 5. 状态和审计

```text
pending -> running -> waiting_approval -> completed
              |              |             |
              |              +-----------> failed
              +--------------------------> cancelled
```

每个任务都有独立事件流，典型事件包括 `task.created`、`task.started`、
`approval.resolved`、`task.retry.requested`、`task.failed` 和 `task.completed`。

## 6. 可观测性接口

| 接口 | 用途 |
| --- | --- |
| `/healthz` | 进程存活探针 |
| `/readyz` | SQLite、Redis 就绪探针 |
| `/v1/tasks/{task_id}/events` | 单任务审计事件 |
| `/v1/tasks/{task_id}/attempts` | 重试父子链 |
| `/v1/metrics` | 成功率、延迟、Token、成本和 Worker 指标 |

## 7. 扩展边界

- API 可以由 Java Spring Boot 网关或内部 BFF 调用，不依赖 Python 内部对象。
- Redis Streams 可以替换为 Kafka、RabbitMQ 等任务队列，保留 SQLite 原子认领契约。
- SQLite 适合单节点演示和小规模部署；多 API/Worker 高并发场景应迁移 PostgreSQL。
- Trace 当前通过 Agent 内部记录收集，后续可以接入 Langfuse、OpenTelemetry 或企业日志平台。
