"""Reusable evaluation runner for the portfolio projects."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .metrics import (
    average_cost,
    average_latency,
    average_steps,
    percentile_latency,
    success_rate,
    tool_error_rate,
)
from .schema import EvaluationResult, RunSummary


@dataclass(frozen=True)
class EvaluationTask:
    """One deterministic input that can be replayed against an agent."""

    task_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvaluationReport:
    results: tuple[EvaluationResult, ...]
    runs: tuple[RunSummary, ...]

    @property
    def success_rate(self) -> float:
        return success_rate(self.results)

    @property
    def average_latency_ms(self) -> float:
        return average_latency(self.runs)

    @property
    def average_steps(self) -> float:
        return average_steps(self.runs)

    @property
    def p50_latency_ms(self) -> float:
        return percentile_latency(self.runs, 0.50)

    @property
    def p95_latency_ms(self) -> float:
        return percentile_latency(self.runs, 0.95)

    @property
    def average_cost(self) -> float:
        return average_cost(self.runs)

    @property
    def tool_error_rate(self) -> float:
        return tool_error_rate(self.runs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "results": [result.to_dict() for result in self.results],
            "runs": [run.to_dict() for run in self.runs],
            "summary": {
                "success_rate": self.success_rate,
                "average_latency_ms": self.average_latency_ms,
                "p50_latency_ms": self.p50_latency_ms,
                "p95_latency_ms": self.p95_latency_ms,
                "average_steps": self.average_steps,
                "average_cost": self.average_cost,
                "tool_error_rate": self.tool_error_rate,
            },
        }


@dataclass(frozen=True)
class EvaluationComparison:
    """Regression-focused comparison between two runs of one evaluation set."""

    baseline_name: str
    candidate_name: str
    baseline: EvaluationReport
    candidate: EvaluationReport
    regressions: tuple[str, ...]
    improvements: tuple[str, ...]

    @property
    def metric_deltas(self) -> dict[str, float]:
        """Return candidate minus baseline for every aggregate metric."""

        return {
            "success_rate": self.candidate.success_rate - self.baseline.success_rate,
            "average_latency_ms": (
                self.candidate.average_latency_ms - self.baseline.average_latency_ms
            ),
            "p50_latency_ms": self.candidate.p50_latency_ms - self.baseline.p50_latency_ms,
            "p95_latency_ms": self.candidate.p95_latency_ms - self.baseline.p95_latency_ms,
            "average_steps": self.candidate.average_steps - self.baseline.average_steps,
            "average_cost": self.candidate.average_cost - self.baseline.average_cost,
            "tool_error_rate": self.candidate.tool_error_rate - self.baseline.tool_error_rate,
        }

    @property
    def passed(self) -> bool:
        return not self.regressions

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_name": self.baseline_name,
            "candidate_name": self.candidate_name,
            "passed": self.passed,
            "regressions": list(self.regressions),
            "improvements": list(self.improvements),
            "metric_deltas": self.metric_deltas,
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
        }


def compare_reports(
    baseline: EvaluationReport,
    candidate: EvaluationReport,
    *,
    baseline_name: str = "baseline",
    candidate_name: str = "candidate",
) -> EvaluationComparison:
    """Compare two reports and fail fast when their task sets differ."""

    baseline_ids = {result.task_id for result in baseline.results}
    candidate_ids = {result.task_id for result in candidate.results}
    if baseline_ids != candidate_ids:
        missing = sorted(baseline_ids - candidate_ids)
        unexpected = sorted(candidate_ids - baseline_ids)
        raise ValueError(
            f"evaluation task sets differ; missing={missing}, unexpected={unexpected}"
        )

    baseline_results = {result.task_id: result for result in baseline.results}
    candidate_results = {result.task_id: result for result in candidate.results}
    regressions = tuple(
        task_id
        for task_id in sorted(baseline_ids)
        if baseline_results[task_id].success and not candidate_results[task_id].success
    )
    improvements = tuple(
        task_id
        for task_id in sorted(baseline_ids)
        if not baseline_results[task_id].success and candidate_results[task_id].success
    )
    return EvaluationComparison(
        baseline_name,
        candidate_name,
        baseline,
        candidate,
        regressions,
        improvements,
    )


EvaluationExecutor = Callable[[EvaluationTask], RunSummary]
EvaluationJudge = Callable[[EvaluationTask, RunSummary], EvaluationResult | bool]


class EvaluationRunner:
    """Run a task set and normalize success, failure, and timing records."""

    def __init__(
        self,
        executor: EvaluationExecutor,
        *,
        judge: EvaluationJudge | None = None,
    ) -> None:
        self.executor = executor
        self.judge = judge

    def run(self, tasks: Iterable[EvaluationTask]) -> EvaluationReport:
        results: list[EvaluationResult] = []
        runs: list[RunSummary] = []
        for task in tasks:
            started = time.perf_counter()
            try:
                summary = self.executor(task)
                if not isinstance(summary, RunSummary):
                    raise TypeError("evaluation executor must return RunSummary")
                result = self._judge(task, summary)
            except (AttributeError, KeyError, OSError, TypeError, ValueError, RuntimeError) as exc:
                summary = RunSummary(
                    run_id=f"eval-{task.task_id}",
                    project=str(task.metadata.get("project", "unknown")),
                    task_id=task.task_id,
                    status="failed",
                    steps=0,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    tool_errors=1,
                )
                result = EvaluationResult(task.task_id, False, f"executor error: {exc}")
            runs.append(summary)
            results.append(result)
        return EvaluationReport(tuple(results), tuple(runs))

    def _judge(self, task: EvaluationTask, summary: RunSummary) -> EvaluationResult:
        if self.judge is None:
            return EvaluationResult(
                task_id=task.task_id,
                success=summary.status == "completed",
                reason=summary.status,
                score=1.0 if summary.status == "completed" else 0.0,
            )
        judged = self.judge(task, summary)
        if isinstance(judged, EvaluationResult):
            if judged.task_id != task.task_id:
                raise ValueError("evaluation judge returned a different task id")
            return judged
        if isinstance(judged, bool):
            return EvaluationResult(task.task_id, judged, "custom judge", float(judged))
        raise TypeError("evaluation judge must return EvaluationResult or bool")


def load_tasks(path: str | Path) -> list[EvaluationTask]:
    """Load the simple JSON task format used by all three projects."""

    source = Path(path)
    with source.open("r", encoding="utf-8") as handle:
        raw_tasks = json.load(handle)
    if not isinstance(raw_tasks, list):
        raise TypeError("evaluation task file must contain a JSON array")

    tasks: list[EvaluationTask] = []
    for raw_task in raw_tasks:
        if not isinstance(raw_task, dict) or not isinstance(raw_task.get("id"), str):
            raise TypeError("each evaluation task needs a string id")
        metadata = raw_task.get("metadata", {})
        if not isinstance(metadata, dict):
            raise TypeError("task metadata must be an object")
        payload = raw_task.get("payload")
        if payload is None:
            payload = {key: value for key, value in raw_task.items() if key not in {"id", "metadata"}}
        if not isinstance(payload, dict):
            raise TypeError("task payload must be an object")
        tasks.append(EvaluationTask(raw_task["id"], dict(payload), dict(metadata)))
    return tasks
