import difflib
import sys
import threading
import time
from pathlib import Path

from agent_shared import ModelRequest, ModelResponse, ScriptedModel, ToolCall
from codepilot.agent import AgentRun, CodePilotAgent
from codepilot.api import ApiPrincipal, create_app
from codepilot.queue import InMemoryTaskQueue
from codepilot.session_store import SqliteApprovalSessionStore
from codepilot.state import CodePilotPhase, CodePilotState
from codepilot.task_manager import CodePilotTaskManager, TaskStatus
from fastapi.testclient import TestClient


def make_patch(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


class BlockingModel:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.started.set()
        self.release.wait(timeout=5)
        return ModelResponse(content="late response")


def make_async_client(
    tmp_path: Path,
    model: object,
    *,
    manager: CodePilotTaskManager,
) -> tuple[TestClient, SqliteApprovalSessionStore]:
    store = SqliteApprovalSessionStore(tmp_path / "approvals.sqlite3")
    app = create_app(
        lambda repo: CodePilotAgent(model, session_store=store),
        store,
        allowed_repo_roots=[tmp_path],
        api_keys={
            "alice-token": ApiPrincipal("alice", frozenset({"developer"})),
            "carol-token": ApiPrincipal("carol", frozenset({"developer"})),
            "reviewer-token": ApiPrincipal("bob", frozenset({"reviewer"})),
        },
        task_manager=manager,
    )
    return TestClient(app), store


def wait_for_task(client: TestClient, task_id: str, expected: set[str]) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(
            f"/v1/tasks/{task_id}",
            headers={"X-API-Key": "alice-token"},
        )
        payload = response.json()
        if payload["status"] in expected:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"task did not reach {expected}: {payload}")


def test_api_runs_and_resolves_persisted_approval(tmp_path: Path):
    source = tmp_path / "service.py"
    before = "def run():\n    return 1\n"
    after = "def run():\n    return 2\n"
    source.write_text(before, encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_service.py").write_text(
        "from service import run\n\n\ndef test_run():\n    assert run() == 2\n",
        encoding="utf-8",
    )
    patch = make_patch("service.py", before, after)
    store = SqliteApprovalSessionStore(tmp_path / "approvals.sqlite3")
    models = [
        ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "patch-1",
                            "propose_patch",
                            {"patch": patch, "paths": ["service.py"]},
                        ),
                    )
                )
            ]
        ),
        ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "tests-1",
                            "run_tests",
                            {"command": [sys.executable, "-m", "pytest", "tests", "-q"]},
                        ),
                    )
                ),
                ModelResponse(content="API 审批后完成。"),
            ]
        ),
    ]

    def agent_factory(repo: Path) -> CodePilotAgent:
        return CodePilotAgent(models.pop(0), session_store=store)

    api_keys = {
        "developer-token": ApiPrincipal("alice", frozenset({"developer"})),
        "reviewer-token": ApiPrincipal("bob", frozenset({"reviewer"})),
    }
    client = TestClient(
        create_app(
            agent_factory,
            store,
            allowed_repo_roots=[tmp_path],
            api_keys=api_keys,
        )
    )

    created = client.post(
        "/v1/runs",
        json={"issue": "change return value", "repo_path": str(tmp_path)},
        headers={"X-API-Key": "developer-token", "Idempotency-Key": "run-1"},
    )

    assert created.status_code == 200
    payload = created.json()
    assert payload["status"] == "waiting_approval"
    assert payload["approval_required"] is True
    session_id = payload["session_id"]

    replayed = client.post(
        "/v1/runs",
        json={"issue": "change return value", "repo_path": str(tmp_path)},
        headers={"X-API-Key": "developer-token", "Idempotency-Key": "run-1"},
    )
    assert replayed.status_code == 200
    assert replayed.json() == payload

    conflict = client.post(
        "/v1/runs",
        json={"issue": "different issue", "repo_path": str(tmp_path)},
        headers={"X-API-Key": "developer-token", "Idempotency-Key": "run-1"},
    )
    assert conflict.status_code == 409

    inspected = client.get(
        f"/v1/approval-sessions/{session_id}",
        headers={"X-API-Key": "reviewer-token"},
    )
    assert inspected.status_code == 200
    assert inspected.json()["patch"] == patch
    assert inspected.json()["created_by"] == "alice"

    completed = client.post(
        f"/v1/approval-sessions/{session_id}/decision",
        json={"approved": True},
        headers={"X-API-Key": "reviewer-token", "Idempotency-Key": "decision-1"},
    )

    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert source.read_text(encoding="utf-8") == after
    resolved = client.get(
        f"/v1/approval-sessions/{session_id}",
        headers={"X-API-Key": "reviewer-token"},
    )
    assert resolved.json()["resolved_by"] == "bob"

    decision_replay = client.post(
        f"/v1/approval-sessions/{session_id}/decision",
        json={"approved": True},
        headers={"X-API-Key": "reviewer-token", "Idempotency-Key": "decision-1"},
    )
    assert decision_replay.status_code == 200
    assert decision_replay.json() == completed.json()


def test_api_rejects_repository_outside_allowlist(tmp_path: Path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    store = SqliteApprovalSessionStore(tmp_path / "approvals.sqlite3")
    app = create_app(
        lambda repo: CodePilotAgent(ScriptedModel([]), session_store=store),
        store,
        allowed_repo_roots=[allowed],
        api_keys={"developer-token": ApiPrincipal("alice", frozenset({"developer"}))},
    )

    response = TestClient(app).post(
        "/v1/runs",
        json={"issue": "inspect", "repo_path": str(outside)},
        headers={"X-API-Key": "developer-token", "Idempotency-Key": "outside-1"},
    )

    assert response.status_code == 403


def test_api_requires_authentication_and_separates_approval_role(tmp_path: Path):
    store = SqliteApprovalSessionStore(tmp_path / "approvals.sqlite3")
    app = create_app(
        lambda repo: CodePilotAgent(ScriptedModel([]), session_store=store),
        store,
        allowed_repo_roots=[tmp_path],
        api_keys={
            "developer-token": ApiPrincipal("alice", frozenset({"developer"})),
            "reviewer-token": ApiPrincipal("bob", frozenset({"reviewer"})),
        },
    )
    client = TestClient(app)

    missing = client.post(
        "/v1/runs",
        json={"issue": "inspect", "repo_path": str(tmp_path)},
    )

    assert missing.status_code == 401

    missing_idempotency = client.post(
        "/v1/runs",
        json={"issue": "inspect", "repo_path": str(tmp_path)},
        headers={"X-API-Key": "developer-token"},
    )
    assert missing_idempotency.status_code == 400

    developer_decision = client.post(
        "/v1/approval-sessions/not-found/decision",
        json={"approved": False},
        headers={"X-API-Key": "developer-token"},
    )

    assert developer_decision.status_code == 403


def test_api_submits_async_task_and_replays_same_request(tmp_path: Path):
    store = SqliteApprovalSessionStore(tmp_path / "approvals.sqlite3")
    manager = CodePilotTaskManager(max_workers=1)
    models = [ScriptedModel([ModelResponse(content="no-op")])]
    app = create_app(
        lambda repo: CodePilotAgent(models.pop(0), session_store=store),
        store,
        allowed_repo_roots=[tmp_path],
        api_keys={"developer-token": ApiPrincipal("alice", frozenset({"developer"}))},
        task_manager=manager,
    )
    client = TestClient(app)
    headers = {"X-API-Key": "developer-token", "Idempotency-Key": "task-1"}

    try:
        created = client.post(
            "/v1/tasks",
            json={"issue": "inspect", "repo_path": str(tmp_path)},
            headers=headers,
        )
        assert created.status_code == 202
        payload = created.json()
        assert payload["status"] in {TaskStatus.PENDING.value, TaskStatus.RUNNING.value}

        replayed = client.post(
            "/v1/tasks",
            json={"issue": "inspect", "repo_path": str(tmp_path)},
            headers=headers,
        )
        assert replayed.status_code == 202
        assert replayed.json()["task_id"] == payload["task_id"]

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = client.get(
                f"/v1/tasks/{payload['task_id']}",
                headers={"X-API-Key": "developer-token"},
            ).json()
            if current["status"] == TaskStatus.FAILED.value:
                break
            time.sleep(0.02)
        assert current["status"] == TaskStatus.FAILED.value
    finally:
        manager.shutdown()


def test_api_task_owner_can_cancel_and_replay_the_request(tmp_path: Path):
    manager = CodePilotTaskManager(max_workers=1)
    model = BlockingModel()
    client, _ = make_async_client(tmp_path, model, manager=manager)
    try:
        created = client.post(
            "/v1/tasks",
            json={"issue": "inspect", "repo_path": str(tmp_path)},
            headers={"X-API-Key": "alice-token", "Idempotency-Key": "task-owner-1"},
        )
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        assert model.started.wait(timeout=2)

        cancelled = client.post(
            f"/v1/tasks/{task_id}/cancel",
            headers={"X-API-Key": "alice-token", "Idempotency-Key": "cancel-owner-1"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["cancel_requested"] is True
        assert cancelled.json()["status"] == TaskStatus.CANCELLING.value

        replayed = client.post(
            f"/v1/tasks/{task_id}/cancel",
            headers={"X-API-Key": "alice-token", "Idempotency-Key": "cancel-owner-1"},
        )
        assert replayed.status_code == 200
        assert replayed.json() == cancelled.json()

        model.release.set()
        completed = wait_for_task(client, task_id, {TaskStatus.CANCELLED.value})
        assert completed["phase"] == CodePilotPhase.CANCELLED.value
    finally:
        model.release.set()
        manager.shutdown()


def test_api_developer_cannot_cancel_another_users_task(tmp_path: Path):
    manager = CodePilotTaskManager(max_workers=1)
    model = BlockingModel()
    client, _ = make_async_client(tmp_path, model, manager=manager)
    try:
        created = client.post(
            "/v1/tasks",
            json={"issue": "inspect", "repo_path": str(tmp_path)},
            headers={"X-API-Key": "alice-token", "Idempotency-Key": "task-owner-2"},
        )
        task_id = created.json()["task_id"]
        assert model.started.wait(timeout=2)

        forbidden = client.post(
            f"/v1/tasks/{task_id}/cancel",
            headers={"X-API-Key": "carol-token", "Idempotency-Key": "cancel-other-1"},
        )
        assert forbidden.status_code == 403

        reviewer_cancelled = client.post(
            f"/v1/tasks/{task_id}/cancel",
            headers={"X-API-Key": "reviewer-token", "Idempotency-Key": "cancel-reviewer-1"},
        )
        assert reviewer_cancelled.status_code == 200
        model.release.set()
        wait_for_task(client, task_id, {TaskStatus.CANCELLED.value})
    finally:
        model.release.set()
        manager.shutdown()


def test_api_retries_failed_task_with_owner_scope_and_idempotency(tmp_path: Path):
    store = SqliteApprovalSessionStore(tmp_path / "approvals.sqlite3")
    manager = CodePilotTaskManager(max_workers=1, store=store)
    models = [
        ScriptedModel([ModelResponse(content="no-op")]),
        ScriptedModel([ModelResponse(content="no-op")]),
    ]

    def agent_factory(repo: Path) -> CodePilotAgent:
        return CodePilotAgent(models.pop(0), session_store=store)

    client = TestClient(
        create_app(
            agent_factory,
            store,
            allowed_repo_roots=[tmp_path],
            api_keys={
                "alice-token": ApiPrincipal("alice", frozenset({"developer"})),
                "carol-token": ApiPrincipal("carol", frozenset({"developer"})),
                "reviewer-token": ApiPrincipal("bob", frozenset({"reviewer"})),
            },
            task_manager=manager,
        )
    )
    try:
        created = client.post(
            "/v1/tasks",
            json={"issue": "inspect", "repo_path": str(tmp_path)},
            headers={"X-API-Key": "alice-token", "Idempotency-Key": "retry-source-1"},
        )
        assert created.status_code == 202
        original_id = created.json()["task_id"]
        failed = wait_for_task(client, original_id, {TaskStatus.FAILED.value})

        forbidden = client.post(
            f"/v1/tasks/{original_id}/retry",
            headers={"X-API-Key": "carol-token", "Idempotency-Key": "retry-forbidden-1"},
        )
        assert forbidden.status_code == 403

        retried = client.post(
            f"/v1/tasks/{original_id}/retry",
            headers={"X-API-Key": "alice-token", "Idempotency-Key": "retry-owner-1"},
        )
        assert retried.status_code == 202
        child = retried.json()
        assert child["task_id"] != original_id
        assert child["parent_task_id"] == original_id
        assert child["retry_count"] == 1

        lineage = client.get(
            f"/v1/tasks/{child['task_id']}/attempts",
            headers={"X-API-Key": "alice-token"},
        )
        assert lineage.status_code == 200
        assert lineage.json()["root_task_id"] == original_id
        assert [item["task_id"] for item in lineage.json()["attempts"]] == [
            original_id,
            child["task_id"],
        ]
        forbidden_lineage = client.get(
            f"/v1/tasks/{child['task_id']}/attempts",
            headers={"X-API-Key": "carol-token"},
        )
        assert forbidden_lineage.status_code == 403
        reviewer_lineage = client.get(
            f"/v1/tasks/{child['task_id']}/attempts",
            headers={"X-API-Key": "reviewer-token"},
        )
        assert reviewer_lineage.status_code == 200

        replayed = client.post(
            f"/v1/tasks/{original_id}/retry",
            headers={"X-API-Key": "alice-token", "Idempotency-Key": "retry-owner-1"},
        )
        assert replayed.status_code == 202
        assert replayed.json() == child

        child_failed = wait_for_task(client, child["task_id"], {TaskStatus.FAILED.value})
        assert child_failed["parent_task_id"] == original_id
        child_events = client.get(
            f"/v1/tasks/{child['task_id']}/events",
            headers={"X-API-Key": "alice-token"},
        )
        assert child_events.status_code == 200
        assert [event["event_type"] for event in child_events.json()["events"]] == [
            "task.created",
            "task.started",
            "task.failed",
        ]
        forbidden_events = client.get(
            f"/v1/tasks/{child['task_id']}/events",
            headers={"X-API-Key": "carol-token"},
        )
        assert forbidden_events.status_code == 403
        assert client.get(
            f"/v1/tasks/{original_id}", headers={"X-API-Key": "alice-token"}
        ).json()["task_id"] == failed["task_id"]
    finally:
        manager.shutdown()


def test_api_external_queue_does_not_construct_agent_in_web_process(tmp_path: Path):
    store = SqliteApprovalSessionStore(tmp_path / "approvals.sqlite3")
    queue = InMemoryTaskQueue()
    manager = CodePilotTaskManager(max_workers=1, store=store, queue=queue)

    def forbidden_factory(repo: Path) -> CodePilotAgent:
        raise AssertionError("web process must not construct an Agent in Redis mode")

    client = TestClient(
        create_app(
            forbidden_factory,
            store,
            allowed_repo_roots=[tmp_path],
            api_keys={"alice-token": ApiPrincipal("alice", frozenset({"developer"}))},
            task_manager=manager,
        )
    )
    try:
        response = client.post(
            "/v1/tasks",
            json={"issue": "queued task", "repo_path": str(tmp_path)},
            headers={"X-API-Key": "alice-token", "Idempotency-Key": "queue-task-1"},
        )
        assert response.status_code == 202
        assert response.json()["status"] == TaskStatus.PENDING.value
        assert queue.read(block_ms=0)[0].task_id == response.json()["task_id"]
    finally:
        manager.shutdown()


def test_api_task_list_is_persisted_and_owner_scoped(tmp_path: Path):
    manager = CodePilotTaskManager(max_workers=1)
    model = BlockingModel()
    client, _ = make_async_client(tmp_path, model, manager=manager)
    try:
        created = client.post(
            "/v1/tasks",
            json={"issue": "private task", "repo_path": str(tmp_path)},
            headers={"X-API-Key": "alice-token", "Idempotency-Key": "task-list-1"},
        )
        assert created.status_code == 202
        task_id = created.json()["task_id"]

        owner_list = client.get("/v1/tasks", headers={"X-API-Key": "alice-token"})
        assert owner_list.status_code == 200
        assert owner_list.json()["count"] == 1
        assert owner_list.json()["tasks"][0]["task_id"] == task_id

        other_detail = client.get(
            f"/v1/tasks/{task_id}",
            headers={"X-API-Key": "carol-token"},
        )
        assert other_detail.status_code == 403
        other_list = client.get("/v1/tasks", headers={"X-API-Key": "carol-token"})
        assert other_list.status_code == 200
        assert other_list.json()["count"] == 0

        reviewer_list = client.get("/v1/tasks", headers={"X-API-Key": "reviewer-token"})
        assert reviewer_list.status_code == 200
        assert reviewer_list.json()["count"] == 1
    finally:
        model.release.set()
        manager.shutdown()


def test_api_reviewer_can_cancel_another_users_task(tmp_path: Path):
    manager = CodePilotTaskManager(max_workers=1)
    model = BlockingModel()
    client, _ = make_async_client(tmp_path, model, manager=manager)
    try:
        created = client.post(
            "/v1/tasks",
            json={"issue": "inspect", "repo_path": str(tmp_path)},
            headers={"X-API-Key": "alice-token", "Idempotency-Key": "task-reviewer-1"},
        )
        task_id = created.json()["task_id"]
        assert model.started.wait(timeout=2)

        cancelled = client.post(
            f"/v1/tasks/{task_id}/cancel",
            headers={"X-API-Key": "reviewer-token", "Idempotency-Key": "cancel-reviewer-2"},
        )
        assert cancelled.status_code == 200
        model.release.set()
        wait_for_task(client, task_id, {TaskStatus.CANCELLED.value})
    finally:
        model.release.set()
        manager.shutdown()


def test_api_rejects_cancelling_a_completed_task(tmp_path: Path):
    class CompletedAgent:
        def run(self, issue: str, repo_path: Path, **_: object) -> AgentRun:
            state = CodePilotState(
                issue=issue,
                repo_path=repo_path,
                phase=CodePilotPhase.COMPLETED,
                patch_applied=True,
                tests_passed=True,
            )
            return AgentRun(state, final_message="done")

    manager = CodePilotTaskManager(max_workers=1)
    store = SqliteApprovalSessionStore(tmp_path / "approvals.sqlite3")
    app = create_app(
        lambda repo: CompletedAgent(),
        store,
        allowed_repo_roots=[tmp_path],
        api_keys={"alice-token": ApiPrincipal("alice", frozenset({"developer"}))},
        task_manager=manager,
    )
    client = TestClient(app)
    try:
        created = client.post(
            "/v1/tasks",
            json={"issue": "already done", "repo_path": str(tmp_path)},
            headers={"X-API-Key": "alice-token", "Idempotency-Key": "task-completed-1"},
        )
        task_id = created.json()["task_id"]
        wait_for_task(client, task_id, {TaskStatus.COMPLETED.value})

        response = client.post(
            f"/v1/tasks/{task_id}/cancel",
            headers={"X-API-Key": "alice-token", "Idempotency-Key": "cancel-completed-1"},
        )
        assert response.status_code == 409
        retry = client.post(
            f"/v1/tasks/{task_id}/retry",
            headers={"X-API-Key": "alice-token", "Idempotency-Key": "retry-completed-1"},
        )
        assert retry.status_code == 409
    finally:
        manager.shutdown()


def test_api_cancelling_waiting_approval_does_not_modify_repository(tmp_path: Path):
    source = tmp_path / "service.py"
    original = "def run():\n    return 1\n"
    source.write_text(original, encoding="utf-8")
    patch = make_patch("service.py", original, "def run():\n    return 2\n")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "patch-1",
                        "propose_patch",
                        {"patch": patch, "paths": ["service.py"]},
                    ),
                )
            )
        ]
    )
    manager = CodePilotTaskManager(max_workers=1)
    client, store = make_async_client(tmp_path, model, manager=manager)
    try:
        created = client.post(
            "/v1/tasks",
            json={"issue": "change return value", "repo_path": str(tmp_path)},
            headers={"X-API-Key": "alice-token", "Idempotency-Key": "task-approval-cancel-1"},
        )
        task_id = created.json()["task_id"]
        waiting = wait_for_task(client, task_id, {TaskStatus.WAITING_APPROVAL.value})
        session_id = waiting["session_id"]
        assert session_id is not None

        retrying = client.post(
            f"/v1/tasks/{task_id}/retry",
            headers={"X-API-Key": "alice-token", "Idempotency-Key": "retry-waiting-1"},
        )
        assert retrying.status_code == 409

        cancelled = client.post(
            f"/v1/tasks/{task_id}/cancel",
            headers={"X-API-Key": "alice-token", "Idempotency-Key": "cancel-approval-1"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == TaskStatus.CANCELLED.value
        retrying = client.post(
            f"/v1/tasks/{task_id}/retry",
            headers={"X-API-Key": "alice-token", "Idempotency-Key": "retry-cancelled-1"},
        )
        assert retrying.status_code == 409
        assert source.read_text(encoding="utf-8") == original
        persisted = store.get(session_id)
        assert persisted is not None
        assert persisted.status == "cancelled"
        assert persisted.state["phase"] == CodePilotPhase.CANCELLED.value
    finally:
        manager.shutdown()


def test_api_approval_updates_the_async_task_record(tmp_path: Path):
    source = tmp_path / "service.py"
    before = "def run():\n    return 1\n"
    after = "def run():\n    return 2\n"
    source.write_text(before, encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_service.py").write_text(
        "from service import run\n\n\ndef test_run():\n    assert run() == 2\n",
        encoding="utf-8",
    )
    patch = make_patch("service.py", before, after)
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "patch-1",
                        "propose_patch",
                        {"patch": patch, "paths": ["service.py"]},
                    ),
                )
            ),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "tests-1",
                        "run_tests",
                        {"command": [sys.executable, "-m", "pytest", "tests", "-q"]},
                    ),
                )
            ),
            ModelResponse(content="completed"),
        ]
    )
    manager = CodePilotTaskManager(max_workers=1)
    client, _ = make_async_client(tmp_path, model, manager=manager)
    try:
        created = client.post(
            "/v1/tasks",
            json={"issue": "change return value", "repo_path": str(tmp_path)},
            headers={"X-API-Key": "alice-token", "Idempotency-Key": "task-approval-1"},
        )
        task_id = created.json()["task_id"]
        waiting = wait_for_task(client, task_id, {TaskStatus.WAITING_APPROVAL.value})
        session_id = waiting["session_id"]

        approved = client.post(
            f"/v1/approval-sessions/{session_id}/decision",
            json={"approved": True},
            headers={"X-API-Key": "reviewer-token", "Idempotency-Key": "decision-task-1"},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == CodePilotPhase.COMPLETED.value
        completed = wait_for_task(client, task_id, {TaskStatus.COMPLETED.value})
        assert completed["phase"] == CodePilotPhase.COMPLETED.value
        assert source.read_text(encoding="utf-8") == after
    finally:
        manager.shutdown()


def test_api_serves_the_codepilot_console(tmp_path: Path):
    store = SqliteApprovalSessionStore(tmp_path / "approvals.sqlite3")
    app = create_app(
        lambda repo: CodePilotAgent(ScriptedModel([]), session_store=store),
        store,
        allowed_repo_roots=[tmp_path],
        api_keys={"developer-token": ApiPrincipal("alice", frozenset({"developer"}))},
    )
    client = TestClient(app)

    page = client.get("/console")
    asset = client.get("/console/assets/app.js")
    root = client.get("/")

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "CodePilot Console" in page.text
    assert asset.status_code == 200
    assert "refreshAll" in asset.text
    assert "taskListSignature" in asset.text
    assert "schedulePolling" in asset.text
    assert "/v1/tasks?limit=50" in asset.text
    assert "/attempts" in asset.text
    assert "/events" in asset.text
    assert "attempt-list" in page.text
    assert "event-list" in page.text
    assert root.status_code == 200


def test_api_exposes_liveness_and_readiness_checks(tmp_path: Path):
    store = SqliteApprovalSessionStore(tmp_path / "readiness.sqlite3")
    app = create_app(
        lambda repo: CodePilotAgent(ScriptedModel([]), session_store=store),
        store,
        allowed_repo_roots=[tmp_path],
        api_keys={"developer-token": ApiPrincipal("alice", frozenset({"developer"}))},
    )
    client = TestClient(app)

    health = client.get("/healthz")
    ready = client.get("/readyz")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok", "checks": {"sqlite": "ok"}}
