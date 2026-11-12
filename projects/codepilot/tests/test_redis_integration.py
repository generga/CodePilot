"""Opt-in real Redis delivery, approval and test-execution acceptance."""

import difflib
import os
import sys
import uuid

import pytest
from agent_shared import ModelResponse, ScriptedModel, ToolCall
from codepilot.agent import CodePilotAgent
from codepilot.api import ApiPrincipal, create_app
from codepilot.queue import RedisTaskQueue
from codepilot.session_store import SqliteApprovalSessionStore
from codepilot.task_manager import CodePilotTaskManager, TaskStatus
from codepilot.worker import CodePilotWorker
from fastapi.testclient import TestClient


@pytest.mark.integration
def test_real_redis_task_approval_and_tests(tmp_path):
    url = os.environ.get("CODEPILOT_TEST_REDIS_URL")
    if not url:
        pytest.skip("set CODEPILOT_TEST_REDIS_URL for a disposable Redis instance")
    redis = pytest.importorskip("redis")
    client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=5)
    stream = "codepilot:acceptance:" + uuid.uuid4().hex
    queue = RedisTaskQueue(url, stream=stream, client=client)
    store = SqliteApprovalSessionStore(tmp_path / "state.sqlite3")
    manager = CodePilotTaskManager(store=store, queue=queue)
    worker_manager = CodePilotTaskManager(
        store=SqliteApprovalSessionStore(tmp_path / "state.sqlite3"), queue=queue,
    )
    source = tmp_path / "service.py"
    before, after = "def run():\n    return 1\n", "def run():\n    return 2\n"
    source.write_text(before, encoding="utf-8")
    (tmp_path / "test_service.py").write_text(
        "from service import run\ndef test_run():\n    assert run() == 2\n", encoding="utf-8"
    )
    patch = "".join(difflib.unified_diff(
        before.splitlines(True), after.splitlines(True),
        fromfile="a/service.py", tofile="b/service.py",
    ))
    models = iter([
        ScriptedModel([ModelResponse(tool_calls=(ToolCall(
            "patch", "propose_patch", {"patch": patch, "paths": ["service.py"]}
        ),))]),
        ScriptedModel([
            ModelResponse(tool_calls=(ToolCall(
                "tests", "run_tests",
                {"command": [sys.executable, "-m", "pytest", "test_service.py", "-q"]},
            ),)),
            ModelResponse(content="Verified"),
        ]),
    ])

    def factory(repo):
        return CodePilotAgent(next(models), session_store=store)

    app = create_app(factory, store, allowed_repo_roots=[tmp_path], api_keys={
        "dev": ApiPrincipal("alice", frozenset({"developer"})),
        "review": ApiPrincipal("bob", frozenset({"reviewer"})),
    }, task_manager=manager)
    worker = CodePilotWorker(queue=queue, task_manager=worker_manager, agent_factory=factory)
    try:
        with TestClient(app) as api:
            response = api.post("/v1/tasks", json={
                "issue": "return two", "repo_path": str(tmp_path),
            }, headers={"X-API-Key": "dev", "Idempotency-Key": "submit"})
            assert response.status_code == 202
            task_id = response.json()["task_id"]
            assert worker.run_once(block_ms=1) == 1
            task = worker_manager.get(task_id)
            assert task.status == TaskStatus.WAITING_APPROVAL
            assert source.read_text(encoding="utf-8") == before
            assert client.xpending(stream, queue.group)["pending"] == 0
            decision = api.post(
                f"/v1/approval-sessions/{task.approval_session_id}/decision",
                json={"approved": True},
                headers={"X-API-Key": "review", "Idempotency-Key": "approve"},
            )
            assert decision.status_code == 200
            assert decision.json()["status"] == "completed"
            assert source.read_text(encoding="utf-8") == after
            assert store.get_task(task_id).status == "completed"
            assert "approval.resolved" in {
                event.event_type for event in store.list_task_events(task_id)
            }
            assert manager.get(task_id).status == TaskStatus.COMPLETED
    finally:
        manager.shutdown()
        worker_manager.shutdown()
        client.delete(stream)
        client.close()
