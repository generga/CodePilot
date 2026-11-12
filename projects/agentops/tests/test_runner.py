from pathlib import Path

import pytest
from agentops.runner import EvaluationRunner, EvaluationTask, compare_reports, load_tasks
from agentops.schema import EvaluationResult, RunSummary


def summary_for(task: EvaluationTask) -> RunSummary:
    return RunSummary(
        run_id=f"run-{task.task_id}",
        project="demo",
        task_id=task.task_id,
        status="completed" if task.payload.get("should_pass") else "failed",
        steps=3,
        latency_ms=100,
    )


def test_runner_builds_report_from_executor():
    tasks = [
        EvaluationTask("pass", {"should_pass": True}),
        EvaluationTask("fail", {"should_pass": False}),
    ]

    report = EvaluationRunner(summary_for).run(tasks)

    assert report.success_rate == 0.5
    assert report.average_latency_ms == 100
    assert report.p50_latency_ms == 100
    assert report.p95_latency_ms == 100
    assert report.average_steps == 3
    assert report.tool_error_rate == 0.0
    assert report.to_dict()["summary"]["success_rate"] == 0.5


def test_runner_supports_custom_judge_and_normalizes_executor_errors():
    tasks = [EvaluationTask("good", {}, {"project": "codepilot"}), EvaluationTask("bad")]

    def executor(task: EvaluationTask) -> RunSummary:
        if task.task_id == "bad":
            raise RuntimeError("fixture unavailable")
        return RunSummary("run-good", "codepilot", task.task_id, "completed", 2, 80)

    report = EvaluationRunner(
        executor,
        judge=lambda task, summary: EvaluationResult(task.task_id, summary.steps == 2, "steps check"),
    ).run(tasks)

    assert report.results[0].success is True
    assert report.results[1].success is False
    assert report.runs[1].tool_errors == 1
    assert report.runs[1].project == "unknown"


def test_runner_normalizes_file_errors_without_stopping_batch():
    tasks = [EvaluationTask("missing"), EvaluationTask("pass", {"should_pass": True})]

    def executor(task: EvaluationTask) -> RunSummary:
        if task.task_id == "missing":
            raise FileNotFoundError("fixture missing")
        return summary_for(task)

    report = EvaluationRunner(executor).run(tasks)

    assert report.results[0].success is False
    assert report.results[1].success is True
    assert report.runs[0].tool_errors == 1


def test_load_tasks_supports_existing_project_json(tmp_path: Path):
    path = tmp_path / "tasks.json"
    path.write_text(
        '[{"id":"task-1","issue":"fix bug","repo":"demo"}]',
        encoding="utf-8",
    )

    tasks = load_tasks(path)

    assert tasks[0].task_id == "task-1"
    assert tasks[0].payload == {"issue": "fix bug", "repo": "demo"}


def test_compare_reports_identifies_regressions_improvements_and_metric_deltas():
    tasks = [EvaluationTask("one"), EvaluationTask("two")]
    baseline = EvaluationRunner(
        lambda task: RunSummary(
            f"baseline-{task.task_id}", "demo", task.task_id, "completed", 4, 100
        )
    ).run(tasks)
    candidate = EvaluationRunner(
        lambda task: RunSummary(
            f"candidate-{task.task_id}",
            "demo",
            task.task_id,
            "completed" if task.task_id == "one" else "failed",
            3,
            80,
        )
    ).run(tasks)

    comparison = compare_reports(baseline, candidate, baseline_name="prompt-v1", candidate_name="prompt-v2")

    assert comparison.passed is False
    assert comparison.regressions == ("two",)
    assert comparison.improvements == ()
    assert comparison.metric_deltas["average_latency_ms"] == -20
    assert comparison.to_dict()["candidate_name"] == "prompt-v2"


def test_compare_reports_rejects_different_task_sets():
    baseline = EvaluationRunner(summary_for).run([EvaluationTask("one")])
    candidate = EvaluationRunner(summary_for).run([EvaluationTask("two")])

    with pytest.raises(ValueError, match="task sets differ"):
        compare_reports(baseline, candidate)
