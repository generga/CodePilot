from pathlib import Path

from agentops.schema import EvaluationResult, RunSummary
from agentops.store import JsonlStore


def test_jsonl_store_round_trip(tmp_path: Path):
    store = JsonlStore(tmp_path / "runs.jsonl")
    store.append(EvaluationResult("task-1", True, "tests passed").to_dict())
    assert store.read_all() == [
        {"task_id": "task-1", "success": True, "reason": "tests passed", "score": 0.0}
    ]


def test_run_summary_is_serializable():
    summary = RunSummary("run-1", "codepilot", "task-1", "completed", 4, 1200)
    assert summary.to_dict()["steps"] == 4
