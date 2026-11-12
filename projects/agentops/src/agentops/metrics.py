"""Small deterministic aggregations for the first evaluation reports."""

from __future__ import annotations

from collections.abc import Iterable

from .schema import EvaluationResult, RunSummary


def success_rate(results: Iterable[EvaluationResult]) -> float:
    values = list(results)
    if not values:
        return 0.0
    return sum(result.success for result in values) / len(values)


def average_latency(runs: Iterable[RunSummary]) -> float:
    values = list(runs)
    if not values:
        return 0.0
    return sum(run.latency_ms for run in values) / len(values)


def average_steps(runs: Iterable[RunSummary]) -> float:
    values = list(runs)
    if not values:
        return 0.0
    return sum(run.steps for run in values) / len(values)


def average_cost(runs: Iterable[RunSummary]) -> float:
    values = list(runs)
    if not values:
        return 0.0
    return sum(run.estimated_cost for run in values) / len(values)


def percentile_latency(runs: Iterable[RunSummary], percentile: float) -> float:
    """Return an interpolated latency percentile in milliseconds."""

    if not 0 <= percentile <= 1:
        raise ValueError("percentile must be between 0 and 1")
    values = sorted(run.latency_ms for run in runs)
    if not values:
        return 0.0
    position = (len(values) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    fraction = position - lower
    return values[lower] + (values[upper] - values[lower]) * fraction


def tool_error_rate(runs: Iterable[RunSummary]) -> float:
    """Calculate errors per tool call, falling back to steps for legacy records."""

    values = list(runs)
    if not values:
        return 0.0
    total_errors = sum(run.tool_errors for run in values)
    total_calls = sum(run.tool_calls for run in values)
    if total_calls == 0:
        total_calls = sum(run.steps for run in values)
    if total_calls == 0:
        return 0.0
    return total_errors / total_calls
