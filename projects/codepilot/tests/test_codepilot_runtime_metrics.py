from datetime import UTC, datetime
from pathlib import Path

from agent_shared import ModelResponse, ScriptedModel
from codepilot.agent import CodePilotAgent
from codepilot.api import ApiPrincipal, create_app
from codepilot.metrics import TaskMetricsSnapshot, summarize
from codepilot.session_store import SqliteApprovalSessionStore
from codepilot.task_manager import CodePilotTaskManager, TaskStatus
from fastapi.testclient import TestClient


def _wait_for_failure(manager: CodePilotTaskManager, task_id: str):
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = manager.get(task_id)
        if record is not None and record.status == TaskStatus.FAILED:
            return record
        time.sleep(0.02)
    raise AssertionError("task did not fail")


def test_task_metrics_persist_model_usage_and_survive_restart(tmp_path: Path):
    store = SqliteApprovalSessionStore(tmp_path / "metrics.sqlite3")
    manager = CodePilotTaskManager(max_workers=1, store=store)
    try:
        record = manager.submit(
            issue="inspect",
            repo_path=tmp_path,
            created_by="alice",
            agent=CodePilotAgent(
                ScriptedModel([ModelResponse(content="stopped", input_tokens=12, output_tokens=7)])
            ),
        )
        failed = _wait_for_failure(manager, record.task_id)
        assert failed.metrics is not None
        assert failed.metrics.model_calls == 1
        assert failed.metrics.input_tokens == 12
        assert failed.metrics.output_tokens == 7
        assert failed.metrics.finished_at is not None
    finally:
        manager.shutdown()

    restored_manager = CodePilotTaskManager(max_workers=1, store=store)
    try:
        summary = restored_manager.metrics_summary(window_minutes=60)
        assert summary["tasks"]["failed"] == 1
        assert summary["model"]["total_tokens"] == 19
    finally:
        restored_manager.shutdown()


def test_metrics_endpoint_returns_runtime_summary(tmp_path: Path):
    store = SqliteApprovalSessionStore(tmp_path / "metrics-api.sqlite3")
    manager = CodePilotTaskManager(max_workers=1, store=store)
    client = TestClient(
        create_app(
            lambda repo: CodePilotAgent(
                ScriptedModel([ModelResponse(content="stopped", input_tokens=4, output_tokens=3)])
            ),
            store,
            allowed_repo_roots=[tmp_path],
            api_keys={"reviewer": ApiPrincipal("bob", frozenset({"reviewer"}))},
            task_manager=manager,
        )
    )
    try:
        created = manager.submit(
            issue="inspect",
            repo_path=tmp_path,
            created_by="bob",
            agent=CodePilotAgent(ScriptedModel([ModelResponse(content="stopped")])),
        )
        _wait_for_failure(manager, created.task_id)
        response = client.get("/v1/metrics?window_minutes=60", headers={"X-API-Key": "reviewer"})
        assert response.status_code == 200
        assert response.json()["tasks"]["failed"] == 1
        assert response.json()["worker"]["throughput_per_hour"] > 0
    finally:
        manager.shutdown()


def test_metrics_summary_calculates_rates_and_latency():
    records = [
        TaskMetricsSnapshot(
            task_id="completed",
            status="completed",
            created_at="2026-08-25T00:00:00+00:00",
            started_at="2026-08-25T00:00:01+00:00",
            finished_at="2026-08-25T00:00:03+00:00",
            queue_wait_ms=1_000,
            execution_duration_ms=2_000,
            model_calls=2,
            input_tokens=10,
            output_tokens=5,
            tool_calls=3,
            test_runs=1,
            tests_passed=True,
            estimated_cost_usd=0.01,
        ),
        TaskMetricsSnapshot(
            task_id="failed",
            status="failed",
            created_at="2026-08-25T00:00:00+00:00",
            started_at="2026-08-25T00:00:02+00:00",
            finished_at="2026-08-25T00:00:04+00:00",
            queue_wait_ms=2_000,
            execution_duration_ms=2_000,
            model_calls=1,
            input_tokens=4,
            output_tokens=2,
            tool_calls=1,
            test_runs=0,
            tests_passed=False,
            estimated_cost_usd=0.02,
        ),
    ]
    summary = summarize(
        records,
        window_minutes=60,
        now=datetime(2026, 8, 25, 0, 10, tzinfo=UTC),
    )
    assert summary["tasks"]["success_rate"] == 0.5
    assert summary["tasks"]["failure_rate"] == 0.5
    assert summary["latency_ms"]["average_queue_wait"] == 1500.0
    assert summary["model"]["total_tokens"] == 21
