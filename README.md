# CodePilot

[![quality](https://github.com/generga/CodePilot/actions/workflows/ci.yml/badge.svg)](https://github.com/generga/CodePilot/actions/workflows/ci.yml)

带人工审批、测试反馈和可恢复审批会话的研发协作 Agent，面向受控的小型 Python 仓库。

输入问题描述和仓库路径，系统搜索和读取代码、提出统一 Diff；人工批准后应用补丁并运行 pytest，保存任务结果和审计事件。

`Issue -> 搜索/读取 -> 提出 Diff -> 等待审批 -> 应用补丁 -> 测试 -> 完成/失败`

## 设计

- FastAPI、Pydantic、Uvicorn 提供 HTTP API 和浏览器控制台。
- ModelClient 适配 DeepSeek 等 OpenAI-compatible 模型；工具通过统一契约注册。
- 有限步数 Agent Loop 携带消息历史，结构化状态记录补丁及测试事实。
- SQLite 保存任务、审批检查点、幂等记录、审计与指标。
- 本地线程池或 Redis Streams 独立 Worker；审批后的继续执行当前由审批接口推进。
- shared 是共享模型/工具契约；随附 AgentOps 模块用于离线评估。

## 离线体验

要求 Python 3.11+、Git。从仓库根目录使用 PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,worker]"
$env:PYTHONPATH = "projects/codepilot/src;projects/agentops/src;shared/src"
python -m codepilot --offline-demo --approve
python -m codepilot.evaluation
```

离线 Demo 在 .runtime 下复制夹具，不需要模型密钥。五条确定性任务验证流程，不代表真实模型修复成功率。

## 启动控制台

复制 .env.example 为 .env，只在本地填写 MODEL_API_KEY。模型配置由程序读取 .env；服务权限配置通过环境变量设置：

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force .runtime | Out-Null
Copy-Item projects/codepilot/fixtures/user-service .runtime/demo-repo -Recurse
$env:CODEPILOT_ALLOWED_REPO_ROOTS = (Resolve-Path .runtime/demo-repo).Path
$env:CODEPILOT_API_KEYS = "dev-local=alice|developer;review-local=bob|reviewer"
$env:CODEPILOT_EXECUTION_MODE = "thread"
python -m uvicorn codepilot.server:app --host 127.0.0.1 --port 8001
```

打开 http://127.0.0.1:8001/console，仓库填写上述副本的绝对路径，执行密钥为 dev-local，审批密钥为 review-local。这些仅为本机演示值。

## 验证和部署

```powershell
python -m pytest
python -m ruff check projects/codepilot/src projects/codepilot/tests projects/agentops/src shared/src
node --check projects/codepilot/web/app.js
```

真实 Redis 测试需设置 CODEPILOT_TEST_REDIS_URL 指向专用实例，未配置时跳过。Compose 启动前，在 .env 设置服务访问密钥，并设置 CODEPILOT_REPO_ROOT 为测试仓库绝对路径，再执行 docker compose up --build -d。容器内仓库路径为 /repos，控制台端口为 8000。

首次发布的 [Linux CI 验证](https://github.com/generga/CodePilot/actions/runs/43216024272)：96 项测试通过，包含真实 Redis 集成；镜像构建和容器内五条确定性评估通过。真实模型质量不包含在该结果中。

## 当前范围

这是受控环境下的预发布项目。任务级沙箱、同仓库并发保护、Worker 租约及崩溃恢复、跨进程取消尚待完善。pytest 会执行仓库代码，命令白名单不构成恶意代码隔离。

阅读 [架构](docs/ARCHITECTURE.md)、[运维](docs/OPERATIONS.md)、[历史验收](docs/ACCEPTANCE.md) 和 [发布说明](CHANGELOG.md)。尚未声明开源许可证，公开阅读不等于授予任意再分发许可。
