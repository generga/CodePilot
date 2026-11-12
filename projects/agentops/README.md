# AgentOps: 评估与可观测平台

## 项目目标

为 CodePilot 和 Knowledge Agent 提供统一的运行记录、评估任务执行、成本统计和策略对比能力。

## MVP 范围

```text
Agent run
  -> trace events
  -> JSONL storage
  -> evaluation runner
  -> aggregate metrics
  -> baseline comparison
```

第一版使用本地 JSONL 文件，不依赖数据库和云服务。`EvaluationRunner` 接收任意项目的任务执行器和可选判定器，统一输出结果、运行记录、成功率和平均延迟；数据契约稳定后，再考虑 SQLite、Web API 和可视化页面。

## 统一记录内容

- run id、project、task id
- model name 和 prompt version
- 工具名称、参数摘要、成功状态和耗时
- 输入输出 Token 和估算成本
- 最终状态、错误和人工审批结果
- 评估任务的成功判定

## MVP 指标

- task success rate
- tool error rate
- average steps
- p50 / p95 latency
- average cost per task
- human approval rate

`EvaluationReport` 已提供成功率、平均步数、平均成本、p50/p95 延迟和工具错误率；`RunSummary.tool_calls` 为可选字段，旧记录缺少调用数时按步数降级计算错误率。

## 当前状态

当前代码包含跨项目 `EvaluationRunner`、JSONL 存储、确定性指标基线和 Trace JSONL 导出；CodePilot 的 5 条任务与 Knowledge Agent 的 3 条任务已经接入离线评估。`compare_reports` 要求两次运行使用同一任务集，并输出回归任务、改善任务以及候选版本相对基线的指标变化。
