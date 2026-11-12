import pytest
from agentops.metrics import (
    average_cost,
    average_latency,
    average_steps,
    percentile_latency,
    success_rate,
    tool_error_rate,
)
from agentops.schema import EvaluationResult, RunSummary


def test_success_rate_and_average_latency():
    results = [EvaluationResult("a", True), EvaluationResult("b", False)]
    runs = [
        RunSummary("1", "p", "a", "completed", 1, 100),
        RunSummary("2", "p", "b", "failed", 2, 300),
    ]
    assert success_rate(results) == 0.5
    assert average_latency(runs) == 200


def test_extended_run_metrics_and_percentiles():
    runs = [
        RunSummary("1", "p", "a", "completed", 2, 100, estimated_cost=0.01, tool_errors=1, tool_calls=4),
        RunSummary("2", "p", "b", "completed", 4, 300, estimated_cost=0.03, tool_calls=2),
        RunSummary("3", "p", "c", "completed", 6, 500, estimated_cost=0.02, tool_calls=4),
    ]

    assert average_steps(runs) == 4
    assert average_cost(runs) == pytest.approx(0.02)
    assert percentile_latency(runs, 0.5) == 300
    assert percentile_latency(runs, 0.95) == 480
    assert tool_error_rate(runs) == pytest.approx(0.1)


def test_percentile_rejects_out_of_range_value():
    with pytest.raises(ValueError, match="between 0 and 1"):
        percentile_latency([], 1.1)
