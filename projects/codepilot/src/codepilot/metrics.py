"""Runtime metrics collected from durable CodePilot task snapshots."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass
class TaskMetrics:
    """Mutable counters for one task attempt."""

    task_id: str
    status: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    queue_wait_ms: int | None = None
    execution_duration_ms: int | None = None
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    test_runs: int = 0
    tests_passed: bool | None = None
    estimated_cost_usd: float = 0.0


@dataclass(frozen=True)
class TaskMetricsSnapshot:
    """SQLite-friendly immutable form of task metrics."""

    task_id: str
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    queue_wait_ms: int | None
    execution_duration_ms: int | None
    model_calls: int
    input_tokens: int
    output_tokens: int
    tool_calls: int
    test_runs: int
    tests_passed: bool | None
    estimated_cost_usd: float


def snapshot(metrics: TaskMetrics) -> TaskMetricsSnapshot:
    return TaskMetricsSnapshot(
        task_id=metrics.task_id,
        status=metrics.status,
        created_at=metrics.created_at,
        started_at=metrics.started_at,
        finished_at=metrics.finished_at,
        queue_wait_ms=metrics.queue_wait_ms,
        execution_duration_ms=metrics.execution_duration_ms,
        model_calls=metrics.model_calls,
        input_tokens=metrics.input_tokens,
        output_tokens=metrics.output_tokens,
        tool_calls=metrics.tool_calls,
        test_runs=metrics.test_runs,
        tests_passed=metrics.tests_passed,
        estimated_cost_usd=metrics.estimated_cost_usd,
    )


def restore(snapshot_value: TaskMetricsSnapshot) -> TaskMetrics:
    return TaskMetrics(**snapshot_value.__dict__)


def collect_trace(metrics: TaskMetrics, events: Iterable[dict[str, Any]]) -> None:
    """Add model/tool counters from a trace segment to a task's totals."""

    for event in events:
        name = event.get("name")
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if name == "model.completed":
            metrics.model_calls += 1
            metrics.input_tokens += _non_negative_int(payload.get("input_tokens"))
            metrics.output_tokens += _non_negative_int(payload.get("output_tokens"))
        elif name == "tool.completed":
            metrics.tool_calls += 1
            if payload.get("tool_name") == "run_tests":
                metrics.test_runs += 1


def finish(metrics: TaskMetrics, *, status: str, finished_at: str) -> None:
    metrics.status = status
    metrics.finished_at = finished_at
    if metrics.started_at is None:
        return
    metrics.execution_duration_ms = max(
        0,
        int((_parse_timestamp(finished_at) - _parse_timestamp(metrics.started_at)).total_seconds() * 1000),
    )


def summarize(
    records: Iterable[TaskMetricsSnapshot],
    *,
    window_minutes: int,
    queue_depth: int = 0,
    running_workers: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build an API-safe summary for a rolling creation/completion window."""

    if window_minutes <= 0:
        raise ValueError("window_minutes must be positive")
    end = now or datetime.now(UTC)
    start = end - timedelta(minutes=window_minutes)
    values = list(records)
    created = [item for item in values if _in_window(item.created_at, start, end)]
    measured = [
        item
        for item in values
        if _in_window(item.created_at, start, end)
        or (item.finished_at is not None and _in_window(item.finished_at, start, end))
    ]
    status_counts = {status: 0 for status in ("pending", "running", "waiting_approval", "completed", "failed", "cancelled")}
    for item in created:
        status_counts[item.status] = status_counts.get(item.status, 0) + 1
    completed = sum(
        item.status == "completed" and item.finished_at is not None and _in_window(item.finished_at, start, end)
        for item in values
    )
    failed = sum(
        item.status == "failed" and item.finished_at is not None and _in_window(item.finished_at, start, end)
        for item in values
    )
    cancelled = sum(
        item.status == "cancelled" and item.finished_at is not None and _in_window(item.finished_at, start, end)
        for item in values
    )
    terminal = completed + failed + cancelled
    queue_waits = [item.queue_wait_ms for item in measured if item.queue_wait_ms is not None]
    durations = [item.execution_duration_ms for item in measured if item.execution_duration_ms is not None]
    input_tokens = sum(item.input_tokens for item in measured)
    output_tokens = sum(item.output_tokens for item in measured)
    throughput = completed + failed + cancelled
    return {
        "window": {
            "minutes": window_minutes,
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
        "tasks": {
            "created": len(created),
            "completed": completed,
            "failed": failed,
            "cancelled": cancelled,
            "pending": status_counts.get("pending", 0),
            "running": status_counts.get("running", 0),
            "waiting_approval": status_counts.get("waiting_approval", 0),
            "success_rate": _rate(completed, terminal),
            "failure_rate": _rate(failed, terminal),
        },
        "latency_ms": {
            "average_queue_wait": _average(queue_waits),
            "average_execution": _average(durations),
        },
        "model": {
            "calls": sum(item.model_calls for item in measured),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "estimated_cost_usd": round(sum(item.estimated_cost_usd for item in measured), 6),
        },
        "worker": {
            "queue_depth": max(0, queue_depth),
            "running_workers": max(0, running_workers),
            "throughput_per_hour": round(throughput * 60 / window_minutes, 2),
        },
    }


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _in_window(value: str, start: datetime, end: datetime) -> bool:
    timestamp = _parse_timestamp(value)
    return start <= timestamp <= end


def _non_negative_int(value: Any) -> int:
    return max(0, int(value or 0))


def _average(values: list[int | float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0
