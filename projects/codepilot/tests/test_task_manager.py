import difflib
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from agent_shared import ModelRequest, ModelResponse, ScriptedModel, ToolCall
from codepilot.agent import CodePilotAgent
from codepilot.session_store import SqliteApprovalSessionStore
from codepilot.task_manager import CodePilotTaskManager, TaskStatus


def wait_for_status(manager: CodePilotTaskManager, task_id: str, expected: set[TaskStatus]):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = manager.get(task_id)
        if record is not None and record.status in expected:
            return record
        time.sleep(0.02)
    raise AssertionError(f"task did not reach {expected}")


def test_task_manager_runs_agent_in_background(tmp_path: Path):
    manager = CodePilotTaskManager(max_workers=1)
    try:
        record = manager.submit(
            issue="inspect",
            repo_path=tmp_path,
            created_by="alice",
            agent=CodePilotAgent(ScriptedModel([ModelResponse(content="no-op")])),
        )

        completed = wait_for_status(manager, record.task_id, {TaskStatus.FAILED})

        assert completed.result is not None
        assert completed.result.state.errors == ["model stopped before a successful test result"]
    finally:
        manager.shutdown()


class BlockingModel:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.started.set()
        self.release.wait(timeout=5)
        return ModelResponse(content="late response")


def test_task_manager_cancels_running_agent_cooperatively(tmp_path: Path):
    manager = CodePilotTaskManager(max_workers=1)
    model = BlockingModel()
    try:
        record = manager.submit(
            issue="cancel me",
            repo_path=tmp_path,
            created_by="alice",
            agent=CodePilotAgent(model),
        )
        assert model.started.wait(timeout=2)

        cancelling = manager.cancel(record.task_id, requested_by="alice")
        assert cancelling.cancel_event.is_set() is True
        model.release.set()
        cancelled = wait_for_status(manager, record.task_id, {TaskStatus.CANCELLED})

        assert cancelled.result is not None
        assert cancelled.result.state.phase.value == "cancelled"
    finally:
        model.release.set()
        manager.shutdown()


def test_task_manager_restores_waiting_approval_after_restart(tmp_path: Path):
    source = tmp_path / "service.py"
    before = "def run():\n    return 1\n"
    after = "def run():\n    return 2\n"
    source.write_text(before, encoding="utf-8")
    patch = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="a/service.py",
            tofile="b/service.py",
        )
    )
    store = SqliteApprovalSessionStore(tmp_path / "approvals.sqlite3")
    first_manager = CodePilotTaskManager(max_workers=1, store=store)
    record = first_manager.submit(
        issue="change return value",
        repo_path=tmp_path,
        created_by="alice",
        agent=CodePilotAgent(
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
            session_store=store,
        ),
    )
    try:
        waiting = wait_for_status(first_manager, record.task_id, {TaskStatus.WAITING_APPROVAL})
        task_id = waiting.task_id
        session_id = waiting.approval_session_id
        assert session_id is not None
    finally:
        first_manager.shutdown()

    second_manager = CodePilotTaskManager(max_workers=1, store=store)
    try:
        restored = second_manager.get(task_id)
        assert restored is not None
        assert restored.status == TaskStatus.WAITING_APPROVAL
        assert restored.approval_session_id == session_id
        assert restored.state is not None

        cancelled = second_manager.cancel(task_id, requested_by="alice")
        assert cancelled.status == TaskStatus.CANCELLED
        assert source.read_text(encoding="utf-8") == before
        persisted = store.get(session_id)
        assert persisted is not None
        assert persisted.status == "cancelled"
    finally:
        second_manager.shutdown()


def test_task_manager_restores_finished_task_after_restart(tmp_path: Path):
    store = SqliteApprovalSessionStore(tmp_path / "approvals.sqlite3")
    first_manager = CodePilotTaskManager(max_workers=1, store=store)
    try:
        record = first_manager.submit(
            issue="inspect",
            repo_path=tmp_path,
            created_by="alice",
            agent=CodePilotAgent(ScriptedModel([ModelResponse(content="no-op")])),
        )
        finished = wait_for_status(first_manager, record.task_id, {TaskStatus.FAILED})
        assert finished.error is None
    finally:
        first_manager.shutdown()

    second_manager = CodePilotTaskManager(max_workers=1, store=store)
    try:
        restored = second_manager.get(record.task_id)
        assert restored is not None
        assert restored.status == TaskStatus.FAILED
        assert restored.state is not None
        assert restored.state.errors == ["model stopped before a successful test result"]
    finally:
        second_manager.shutdown()


def test_task_manager_retries_failed_task_as_a_new_persisted_attempt(tmp_path: Path):
    store = SqliteApprovalSessionStore(tmp_path / "approvals.sqlite3")
    manager = CodePilotTaskManager(max_workers=1, store=store)
    try:
        original = manager.submit(
            issue="inspect",
            repo_path=tmp_path,
            created_by="alice",
            agent=CodePilotAgent(ScriptedModel([ModelResponse(content="no-op")])),
        )
        failed = wait_for_status(manager, original.task_id, {TaskStatus.FAILED})

        retried = manager.retry(
            original.task_id,
            agent=CodePilotAgent(ScriptedModel([ModelResponse(content="no-op")])),
        )
        finished = wait_for_status(manager, retried.task_id, {TaskStatus.FAILED})

        assert failed.status == TaskStatus.FAILED
        assert finished.task_id != failed.task_id
        assert finished.parent_task_id == failed.task_id
        assert finished.retry_count == 1
        persisted = store.get_task(finished.task_id)
        assert persisted is not None
        assert persisted.parent_task_id == failed.task_id
        assert persisted.retry_count == 1
        assert store.get_task(failed.task_id).status == TaskStatus.FAILED
        lineage = manager.attempts(finished.task_id)
        assert [attempt.task_id for attempt in lineage] == [failed.task_id, finished.task_id]
        event_types = [event.event_type for event in store.list_task_events(finished.task_id)]
        assert event_types == ["task.created", "task.started", "task.failed"]
    finally:
        manager.shutdown()


def test_task_manager_rejects_retry_for_non_failed_tasks(tmp_path: Path):
    manager = CodePilotTaskManager(max_workers=1)
    try:
        record = manager.submit(
            issue="inspect",
            repo_path=tmp_path,
            created_by="alice",
            agent=CodePilotAgent(ScriptedModel([ModelResponse(content="no-op")])),
        )
        with pytest.raises(RuntimeError, match="only failed"):
            manager.retry(record.task_id, agent=CodePilotAgent(ScriptedModel([])))
    finally:
        manager.shutdown()


def test_sqlite_store_migrates_legacy_task_rows_for_retry_metadata(tmp_path: Path):
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                issue TEXT NOT NULL,
                repo_path TEXT NOT NULL,
                created_by TEXT NOT NULL,
                status TEXT NOT NULL,
                state_json TEXT,
                error TEXT,
                approval_session_id TEXT,
                final_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO tasks (
                task_id, issue, repo_path, created_by, status,
                final_message, created_at, updated_at
            ) VALUES ('legacy-task', 'inspect', ?, 'alice', 'failed', '', 'a', 'b')
            """,
            (str(tmp_path),),
        )

    store = SqliteApprovalSessionStore(database)
    snapshot = store.get_task("legacy-task")
    assert snapshot is not None
    assert snapshot.retry_count == 0
    assert snapshot.parent_task_id is None
    event = store.append_task_event(
        event_id="legacy-event",
        task_id="legacy-task",
        event_type="task.migrated",
        actor="system",
        detail={"source": "legacy"},
    )
    assert event.detail == {"source": "legacy"}
    assert store.list_task_events("legacy-task")[0].event_type == "task.migrated"
