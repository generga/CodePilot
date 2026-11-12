# CodePilot 收尾验收记录

验收日期：2026-09-27。范围仅限 CodePilot 和 shared，不代表 Knowledge Agent 验收完成。

## 当前结论

核心研发协作流程已有可复现的自动化验证；本轮补齐容器运行依赖，并修复队列确认和状态同步缺陷。
当前仍是受控环境下的项目版本，**不是生产就绪版本**。下面的上线阻塞项未关闭前，不部署到不可信仓库或多人生产环境。

## 本轮修复

1. Docker 镜像补齐 Git 和 pytest，否则容器中的代码检查、测试命令无法运行。
2. Docker 构建上下文保留 CodePilot 离线评估数据，避免镜像内找不到任务集。
3. Worker 不再在数据库读取异常或任务仍由其他 Worker 执行时确认 Redis 消息。
4. 外部队列模式下，任务详情、列表和指标从 SQLite 刷新，不再一直返回 API 内存里的 pending 状态。
5. 执行者保留正在运行的本地任务对象，避免查询替换 Agent 正在使用的状态；执行结束后释放本地所有权标记。
6. 新队列消费者启动时，不再仅凭 running/cancelling 状态把其他执行者的任务改成失败。本地所有权标记不是分布式租约，也不能判断远端执行者是否存活。
7. 审批完成后刷新任务列表再匹配会话，使独立 Worker 产生的审批结果可以回写任务状态。

## 验证证据

| 检查 | 本轮结果 | 边界 |
| --- | --- | --- |
| CodePilot + shared pytest | 85 passed | 开启真实 Redis 集成测试；不是整个组合仓库的测试总数 |
| 新增状态同步及启动测试 | 修复前 4 failed，修复后通过 | 覆盖详情、列表、指标及新消费者启动 |
| Redis 审批闭环 | 通过 | API 与 Worker 使用独立 manager/store，共享临时 SQLite；仍在同一 Python 测试进程，不是完整 Compose 多进程验收 |
| 离线评估 | 本机及容器均为 5/5 | ScriptedModel 的确定性回归，不代表 DeepSeek 真实修复成功率 |
| Ruff | 通过 | CodePilot 源码、测试及 shared 源码 |
| 前端 JS 语法 | 通过 | 不代替浏览器交互和视觉验收 |
| Docker | 镜像构建及只读根文件系统下的离线流程通过 | 非 root 用户；不等于任务级安全沙箱 |
| CI 配置 | 增加真实 Redis 与镜像内流程检查 | 本轮未在远端 GitHub Actions 执行 |

独立代码复核后，额外加强了成功/失败/终态重复投递的消息确认断言、未提前刷新 API 缓存的审批回写检查，以及活跃任务查询和本地取消的状态对象保护测试。

## 本地复验

在 `D:\llm-agent-portfolio` 使用 PowerShell，先准备专用测试 Redis；不要指向生产实例。

```powershell
$env:CODEPILOT_TEST_REDIS_URL = 'redis://127.0.0.1:16389/0'
.\.venv\Scripts\python.exe -m pytest projects/codepilot/tests shared/tests -o addopts='' -q
.\.venv\Scripts\ruff.exe check projects/codepilot/src projects/codepilot/tests shared/src
node --check projects/codepilot/web/app.js
$env:PYTHONPATH = 'projects/codepilot/src;projects/agentops/src;shared/src'
.\.venv\Scripts\python.exe -m codepilot.evaluation
```

不设置测试 Redis 地址时，相应集成测试会跳过，不能据此声称真实队列验收通过。测试使用唯一 stream，并在结束后删除自己的 stream。

```powershell
docker build -t codepilot:acceptance-20260927 .
docker run --rm --read-only --tmpfs /tmp -e PYTHONPATH=/app/projects/codepilot/src:/app/projects/agentops/src:/app/shared/src codepilot:acceptance-20260927 python -m codepilot.evaluation
docker run --rm codepilot:acceptance-20260927 git --version
docker run --rm codepilot:acceptance-20260927 python -m pytest --version
```

## 尚未关闭的上线阻塞项

### P0：执行隔离与仓库并发保护

仓库 pytest 会执行仓库代码。路径白名单、人工审批、非 root 和只读容器根目录，不等于防恶意代码沙箱。需要任务级隔离、凭据隔离、网络和资源限制，并避免两个任务同时修改同一仓库。验收应包含恶意测试代码和并发写入场景。

### P1：Worker 崩溃恢复

目前保留 running/cancelling 以避免误伤活跃执行者，但执行者真的死亡时任务可能滞留。Redis 消息重新投递本身不能安全恢复有文件副作用的 Agent。下一步需要持久化执行者身份、租约/心跳、过期条件和防旧执行者回写机制，再验证 kill/restart、长任务、重复投递。

在该能力完成前，不手工重置 running 为 pending，也不删除 Redis 消息。先停止任务入口，确认执行者已退出，保存任务记录、Trace 和仓库 Diff，人工核查副作用后再决定恢复方式。

### P1：跨进程取消与状态竞争

本地线程的协作式取消已有测试，不能推断 API 进程的取消能及时传递给独立 Worker。需要持久化取消意图、Worker 检查点，以及取消与完成竞争时的条件更新；验证排队中、模型请求中、测试执行中、审批等待中四类取消。

### P1：完整部署与真实模型验收

需要实际 Compose API/Worker 分进程启动，使用受控仓库完成提交、审批、测试、重试、取消及进程故障流程。本轮没有重新调用 DeepSeek，不提供真实模型质量或成本结论。

### P2：发布证据

补齐并发压测、SQLite 备份恢复演练、浏览器端到端测试和告警演练。真实模型评估需记录任务集、模型配置、耗时、tokens、成本及失败分类。当前仓库没有 Git 提交历史，尚未创建发布 tag 或远端 CI 验收记录。

## 后续执行顺序

1. 明确执行所有权，完成租约恢复与跨进程取消的状态协议。
2. 完成任务执行隔离、仓库并发保护及安全测试。
3. 跑真实 Compose 多进程与 DeepSeek 端到端验收。
4. 完成压测、备份恢复、前端验收，再整理演示脚本和面试材料。

只有对应验收证据通过，才将该项从“计划”改为“已完成”。
