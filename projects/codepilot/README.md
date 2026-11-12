# CodePilot: 研发协作 Agent

独立发布版的启动方式见[仓库首页](../../README.md)，当前限制见[验收记录](../../docs/ACCEPTANCE.md)。本文保留原工作区演示记录和路径；相关历史说明不代表完整生产验收。

## 项目目标

输入一个本地 Git 仓库和一个 Issue，Agent 完成代码定位、修改计划、补丁生成、人工审批和测试验证。

## MVP 范围

```text
Issue
  -> plan
  -> search_code
  -> read_file
  -> propose_patch
  -> human_approval
  -> apply_patch
  -> run_tests
  -> completed / retry / failed
```

第一版只支持本地小型 Python 仓库和固定的测试命令。目标不是自动合并代码，而是提供可检查的修改建议和验证结果。

## 独立测试仓库

已准备一个独立的 Git 测试仓库：`D:\codepilot-demo-repo`。

它当前停留在故意有缺陷的基线提交，运行测试会得到 1 个通过、1 个失败：

```powershell
cd D:\codepilot-demo-repo
& D:\llm-agent-portfolio\.venv\Scripts\python.exe -m pytest
```

用 DeepSeek 运行 CodePilot：

```powershell
cd D:\llm-agent-portfolio
$env:PYTHONPATH = "projects\codepilot\src;shared\src"
& .\.venv\Scripts\python.exe -m codepilot `
  --repo D:\codepilot-demo-repo `
  --issue "修复用户注册时邮箱为空导致的异常"
```

首次运行不要加 `--approve`，Agent 会打印待审批 Diff 并停在 `waiting_approval`，不会修改仓库。确认 Diff 后重新执行同一命令并增加 `--approve`，让本地演示继续应用补丁和运行测试。

## 暂不做

- 自动创建或合并 Pull Request
- 任意 Shell 命令执行
- 无限制的自主循环
- 多 Agent 协作
- 大规模仓库的全量索引

## 工具契约

| 工具 | 副作用 | MVP 规则 |
| --- | --- | --- |
| `search_code` | 无 | 只读，限制结果数量 |
| `read_file` | 无 | 只能读取仓库目录内文件 |
| `get_git_diff` | 无 | 只读 |
| `apply_patch` | 有 | 人工批准后执行 |
| `run_tests` | 有限 | 命令白名单和超时 |

## 当前状态

工具层 MVP 已完成：代码搜索、文件读取、统一补丁校验与应用、路径限制、人工审批门槛、pytest 白名单执行和超时结果都已实现，并有确定性测试覆盖。

模型适配器和最小状态循环也已接入：模型通过统一工具协议搜索、读取、提出补丁和请求测试；没有审批回调时流程会停在 `WAITING_APPROVAL`。

当前企业化增强已经覆盖：审批会话 SQLite 持久化、进程重启后恢复、原子化审批决策、补丁声明路径隔离、API Key/RBAC、请求幂等，以及 FastAPI HTTP 接口。审批前不会写入仓库，审批拒绝或取消不会产生文件变更。

运行指标已经持久化到同一个 SQLite 文件，覆盖任务成功率、失败率、平均排队等待、平均执行时长、模型调用次数、输入/输出 Token、可选成本估算、Worker 吞吐量和队列深度。指标接口为 `GET /v1/metrics?window_minutes=60`，沿用 reviewer/admin/developer 的 API Key 鉴权；控制台顶部会显示最近 60 分钟摘要。

仓库级 CI 会执行 CodePilot 全量测试、Ruff、控制台 JavaScript 语法检查以及 Docker Compose 配置和镜像构建校验。

异步任务接口用于把模型执行从 HTTP 请求中解耦：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/v1/tasks` | 创建异步 Agent 任务，返回 `202` 和 `task_id` |
| `GET` | `/v1/tasks` | 查询当前用户任务，reviewer/admin 可查询全部任务 |
| `GET` | `/v1/tasks/{task_id}` | 查询任务状态、阶段、错误和最终结果 |
| `GET` | `/v1/tasks/{task_id}/attempts` | 查询任务完整重试尝试链 |
| `GET` | `/v1/tasks/{task_id}/events` | 查询任务审计事件和生命周期日志 |
| `GET` | `/v1/metrics` | 查询滚动时间窗口内的运行、模型和 Worker 指标 |
| `POST` | `/v1/tasks/{task_id}/cancel` | 由创建者或 reviewer/admin 请求取消任务 |
| `POST` | `/v1/tasks/{task_id}/retry` | 由创建者或 reviewer/admin 重新执行失败任务 |

任务生命周期为：

```text
pending -> running -> waiting_approval -> completed / failed / cancelled
                         |
                         +-> cancelling -> cancelled
```

只有 `failed` 任务允许重试。每次重试都会创建新的任务 ID，并通过 `parent_task_id`
和 `retry_count` 保留尝试链；原任务的错误、测试输出和审计记录不会被覆盖。重试接口
同样要求 `Idempotency-Key`，网络超时后的重复请求只会回放同一个子任务。
任务详情页会显示完整尝试链，便于 reviewer 对比原始失败和后续重试结果。
任务事件以追加方式写入 SQLite，覆盖创建、开始、审批、取消、重试、失败和完成等关键节点；事件查询默认限制数量，并沿用任务所有者和 reviewer/admin 的访问控制。

本地线程模式的取消是协作式的：模型调用返回后检查取消信号，pytest 执行器检查取消并终止子进程。独立 Worker 的跨进程取消尚未完善。任务及审批持久化到 SQLite，等待审批会话可以恢复；Redis 模式中新消费者不会将已有 running 任务直接标记失败，真正崩溃的任务也可能滞留，尚需租约恢复机制。

本地默认使用进程内 `ThreadPoolExecutor`。企业部署可以设置 `CODEPILOT_EXECUTION_MODE=redis`，让 API 只写入任务记录并投递 Redis Stream，由独立 Worker 创建模型并执行。Worker 使用 consumer group、SQLite 原子认领和 pending message reclaim，避免重复投递造成重复执行。Worker 初始化模型客户端失败时支持有限次数的指数退避重试，默认最多 2 次；模型执行过程中的业务失败不会自动重复修改仓库，仍由 reviewer 或任务所有者通过重试接口触发。Docker Compose 已提供 API、Worker、Redis 和两个持久化数据卷。

离线评估入口已经接入 5 条固定任务：每条任务会复制独立夹具仓库，执行完整的搜索、读取、补丁、审批和测试流程，并由 AgentOps 统一汇总成功率、步数、延迟和工具错误。工具层不会直接依赖任何模型 SDK。

DeepSeek 真实联调已经验证：首次运行会停在 `waiting_approval` 并输出 Diff；在隔离副本中批准后成功应用补丁，仓库测试结果为 `2 passed`。原始 `D:\codepilot-demo-repo` 保持基线不变，便于重复演示。

## 工具层示例

```python
from codepilot.tools import apply_patch, run_tests

apply_patch(repo, patch, approved=True, allowed_paths=["user/service.py"])
result = run_tests(repo, [sys.executable, "-m", "pytest", "tests/test_user.py", "-q"])
assert result.passed
```

## 当前代码结构

```text
src/codepilot/
├── agent.py       # 模型驱动循环、工具分发、审批暂停
├── api.py         # FastAPI 接口、鉴权、RBAC 和幂等编排
├── cli.py         # 离线 Demo 和真实模型入口
├── evaluation.py  # 5 条任务的离线评估入口
├── server.py      # Uvicorn 配置入口
├── queue.py       # In-memory/Redis Streams 任务队列适配器
├── worker.py      # 独立 Worker 消费和执行任务
├── session_store.py # SQLite 会话、审计主体和幂等记录
├── state.py       # 运行状态和阶段
├── tools.py       # 搜索、读取、补丁、测试、Git diff
└── web/           # FastAPI 直接提供的浏览器控制台
```

## 运行离线 Demo

```powershell
cd D:\llm-agent-portfolio
$env:PYTHONPATH = "projects\codepilot\src;shared\src"
\.venv\Scripts\python.exe -m codepilot --offline-demo --approve

# 可选：把本次运行写入 JSONL Trace
\.venv\Scripts\python.exe -m codepilot --offline-demo --approve `
  --trace-file .runtime\traces\codepilot-offline.jsonl
```

历史真实测试用例见 [测试用例](../../docs/TEST_CASE.md)，覆盖邮箱空值修复、Diff 审批和 `2 passed` 测试结果。

## 启动 HTTP API

API 依赖已包含在开发安装中。服务启动前必须显式配置允许访问的仓库根目录，不配置白名单时服务不会启动：

```powershell
$env:CODEPILOT_ALLOWED_REPO_ROOTS = "D:\codepilot-demo-repo;D:\team-repos"
$env:CODEPILOT_SESSION_DB = "D:\llm-agent-portfolio\.runtime\codepilot\approvals.sqlite3"
# 格式：api-key=subject|role[,role...];另一个-key=subject|role
$env:CODEPILOT_API_KEYS = "dev-local=alice|developer;review-local=bob|reviewer"
$env:PYTHONPATH = "projects\codepilot\src;shared\src"
\.venv\Scripts\python.exe -m uvicorn codepilot.server:app --host 127.0.0.1 --port 8000
```

启动后打开 `http://127.0.0.1:8000/console` 使用浏览器控制台。页面可以提交异步任务、轮询执行状态、查看待审批 Diff、提交 reviewer 决策、取消任务，并显示相关文件和测试输出。执行 API Key 需要 `developer/admin` 角色；批准或拒绝补丁时，审批 API Key 需要 `reviewer/admin` 角色。

### Redis Worker 模式

本地启动 Redis 后，API 和 Worker 分别运行：

```powershell
$env:CODEPILOT_EXECUTION_MODE = "redis"
$env:REDIS_URL = "redis://127.0.0.1:6379/0"
\.venv\Scripts\python.exe -m uvicorn codepilot.server:app --host 127.0.0.1 --port 8000

\.venv\Scripts\python.exe -m codepilot.worker
```

更完整的部署直接使用：

```powershell
$env:CODEPILOT_REPO_ROOT = "D:\codepilot-demo-repo"
docker compose up --build
```

API、Worker 和 Redis 分离运行；`codepilot-data` 保存 SQLite，`codepilot-redis` 保存 Redis AOF。Worker 通过 `CODEPILOT_WORKER_NAME` 可配置 consumer 名称，多个 Worker 可以共享同一个 consumer group。

核心接口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/healthz` | 健康检查 |
| `GET` | `/readyz` | 检查 SQLite 和 Redis 等运行依赖是否就绪 |
| `POST` | `/v1/runs` | 创建 Agent 运行，可能返回 `waiting_approval` |
| `GET` | `/v1/approval-sessions/{session_id}` | 查看待审批补丁和审计信息 |
| `POST` | `/v1/approval-sessions/{session_id}/decision` | 提交批准或拒绝并恢复运行 |

业务请求需要 `X-API-Key`；创建运行和查看会话分别需要 `developer/admin`、`developer/reviewer/admin`，审批决策只允许 `reviewer/admin`。两个写接口都要求 `Idempotency-Key`：相同主体和请求参数会回放第一次结果，复用 Key 但修改参数会返回 `409`。

API 层负责请求校验、鉴权、仓库白名单、幂等和会话编排；模型、工具和审批状态机仍由 CodePilot 核心负责，后续可由 Java Spring Boot 网关调用这些接口。

## Docker 启动

Docker 部署要求 `.env` 中配置 `MODEL_API_KEY`、`CODEPILOT_API_KEYS`，并在 PowerShell 中指定待管理仓库目录：

```powershell
$env:CODEPILOT_REPO_ROOT = "D:\codepilot-demo-repo"
docker compose up --build
```

容器内只允许访问挂载到 `/repos` 的目录，SQLite 数据写入 `/data` 卷；镜像不以 root 用户运行，根文件系统设置为只读。

运行 5 条离线评估任务：

```powershell
$env:PYTHONPATH = "projects\codepilot\src;projects\agentops\src;shared\src"
\.venv\Scripts\python.exe -m codepilot.evaluation
\.venv\Scripts\python.exe -m codepilot.evaluation --json
```

真实 DeepSeek 模式先在工作区根目录准备 `.env`（不要提交）：

```powershell
Copy-Item .env.example .env
# 编辑 .env，填入 MODEL_API_KEY=你的 DeepSeek API Key
```

程序会自动读取当前工作目录下的 `.env`，系统环境变量优先级更高。

默认配置为 `https://api.deepseek.com`、`deepseek-chat` 和 60 秒超时。然后执行：

```powershell
$env:PYTHONPATH = "projects\codepilot\src;shared\src"
python -m codepilot --repo D:\path\to\repo --issue "修复具体问题"
```

真实模型调用会继续经过同一个 OpenAI-compatible 客户端，工具白名单、补丁审批和测试限制不因更换模型而改变。
增加 `--trace-file .runtime\traces\codepilot-deepseek.jsonl` 可以保存本次真实运行的模型、工具、审批、错误和最终状态事件。

## 简历指标

当前 5 条离线任务全部通过，并完成一次 DeepSeek 真实模型冒烟测试；继续记录任务成功率、平均执行步数、工具错误率、延迟和成本，比较不同模型和提示策略。
