"""Schemas for run summaries and evaluation results."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    project: str
    task_id: str
    status: str
    steps: int
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0
    tool_errors: int = 0
    tool_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvaluationResult:
    task_id: str
    success: bool
    reason: str = ""
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
