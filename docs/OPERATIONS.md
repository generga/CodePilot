# CodePilot 运维手册

## 启动前检查

1. 复制 `.env.example` 为本地 `.env`，填入模型 Key、API Key 和允许的仓库目录。
2. 确认 `CODEPILOT_ALLOWED_REPO_ROOTS` 与容器内路径 `/repos` 对应。
3. 确认待管理仓库以读写方式挂载，SQLite 数据卷保持持久化。
4. 使用 Redis 模式时，初始任务交给 Worker；审批后的继续执行当前由 API 推进。生产使用前需关闭 ACCEPTANCE.md 中列出的阻塞项。

## Compose 启动

```powershell
$env:CODEPILOT_REPO_ROOT = "D:\codepilot-demo-repo"
docker compose up --build -d
docker compose ps
docker compose logs --tail=100 codepilot-api codepilot-worker redis
```

## 健康检查

```powershell
Invoke-WebRequest http://127.0.0.1:8000/healthz
Invoke-WebRequest http://127.0.0.1:8000/readyz
```

`healthz` 返回失败通常表示进程没有启动；`readyz` 返回 `503` 时，优先检查 SQLite 数据卷、Redis 容器和 `REDIS_URL`。

## 指标检查

```powershell
$headers = @{ "X-API-Key" = "<reviewer-or-admin-key>" }
Invoke-RestMethod "http://127.0.0.1:8000/v1/metrics?window_minutes=60" -Headers $headers
```

重点关注：

- `tasks.failure_rate`：任务失败比例
- `latency_ms.average_queue_wait`：队列拥塞信号
- `model.total_tokens` 和 `model.estimated_cost_usd`：模型消耗
- `worker.queue_depth`：Redis Stream 积压信号

## 故障处理

### Worker 初始化模型失败

Worker 默认对模型客户端初始化失败执行最多 2 次指数退避重试。重试次数和退避参数由以下环境变量控制：

```text
CODEPILOT_WORKER_MAX_SETUP_RETRIES=2
CODEPILOT_WORKER_RETRY_BACKOFF_SECONDS=1
CODEPILOT_WORKER_MAX_RETRY_BACKOFF_SECONDS=30
```

Agent 已经开始执行后的失败不会自动重放。先查看任务事件和 Diff，再由 reviewer 或任务所有者调用任务重试接口。

### 任务长期 pending

1. 查看 Worker 日志和 `/readyz`。
2. 确认 API 与 Worker 使用同一个 SQLite 数据卷。
3. 确认 API 和 Worker 使用同一个 Redis Stream、Consumer Group 和 Redis 地址。
4. 检查 Redis pending 消息是否持续增长。

### 任务长期 running

Worker 可能在模型请求、测试命令或进程退出期间中断。外部队列模式下，新进程启动不会把其他 Worker 的 running/cancelling 任务直接判为失败；真正中断的任务也可能因此滞留。当前尚无持久化执行租约和安全的过期恢复流程，Redis pending reclaim 不等于 Agent 可安全重放。

先停止任务入口、确认执行者状态，保存任务事件、Trace 和仓库 Diff。不要直接改数据库状态或删除 pending 消息来强制重跑。上线前必须完成故障恢复验收，具体阻塞项见 `ACCEPTANCE.md`。

### SQLite locked

确认只有 API 和 Worker 共享数据卷，不要在多个独立目录中复制 SQLite 文件。高并发部署应迁移 PostgreSQL，并保留任务状态和事件的事务边界。

## 停止和升级

```powershell
docker compose stop codepilot-worker
docker compose up --build -d codepilot-worker
docker compose up --build -d codepilot-api
```

升级前保留 `codepilot-data` 和 `codepilot-redis` 卷；不要删除卷来解决业务故障，否则会丢失审批会话、审计事件和任务指标。
