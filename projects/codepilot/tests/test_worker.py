import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from agent_shared import ModelResponse, ScriptedModel
from codepilot.agent import CodePilotAgent
from codepilot.queue import InMemoryTaskQueue, RedisTaskQueue
from codepilot.session_store import SqliteApprovalSessionStore
from codepilot.task_manager import CodePilotTaskManager, TaskStatus
from codepilot.worker import CodePilotWorker


def test_worker_consumes_external_task_and_acknowledges_message(tmp_path: Path):
    store = SqliteApprovalSessionStore(tmp_path / "approvals.sqlite3")
    queue = InMemoryTaskQueue()
    manager = CodePilotTaskManager(max_workers=1, store=store, queue=queue)
    worker = CodePilotWorker(
        queue=queue,
        task_manager=manager,
        agent_factory=lambda repo: CodePilotAgent(
            ScriptedModel([ModelResponse(content="no-op")]),
            session_store=store,
        ),
    )
    try:
        record = manager.submit(
            issue="inspect",
            repo_path=tmp_path,
            created_by="alice",
        )
        assert record.status == TaskStatus.PENDING
        assert worker.run_once(block_ms=0) == 1
        assert not queue._in_flight
        assert manager.get(record.task_id).status == TaskStatus.FAILED
        assert worker.run_once(block_ms=0) == 0
        assert [event.event_type for event in store.list_task_events(record.task_id)] == [
            "task.created",
            "task.started",
            "task.failed",
        ]
    finally:
        manager.shutdown()


def test_worker_retries_transient_agent_setup_failure_with_backoff_policy(tmp_path: Path):
    store = SqliteApprovalSessionStore(tmp_path / "setup-retry.sqlite3")
    queue = InMemoryTaskQueue()
    manager = CodePilotTaskManager(max_workers=1, store=store, queue=queue)
    attempts = 0

    def factory(repo: Path) -> CodePilotAgent:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary model client failure")
        return CodePilotAgent(
            ScriptedModel([ModelResponse(content="no-op")]),
            session_store=store,
        )

    worker = CodePilotWorker(
        queue=queue,
        task_manager=manager,
        agent_factory=factory,
        max_setup_retries=1,
        retry_backoff_seconds=0,
    )
    try:
        original = manager.submit(issue="retry setup", repo_path=tmp_path, created_by="alice")
        assert worker.run_once(block_ms=0) == 0
        child = manager.attempts(original.task_id)[-1]
        assert not queue._in_flight
        assert child.task_id != original.task_id
        assert child.retry_count == 1
        assert child.status == TaskStatus.PENDING
        assert worker.run_once(block_ms=0) == 1
        assert manager.get(child.task_id).status == TaskStatus.FAILED
        assert attempts == 2
    finally:
        manager.shutdown()


def test_external_manager_keeps_pending_tasks_during_worker_restart(tmp_path: Path):
    store = SqliteApprovalSessionStore(tmp_path / "approvals.sqlite3")
    first_queue = InMemoryTaskQueue()
    first_manager = CodePilotTaskManager(
        max_workers=1,
        store=store,
        queue=first_queue,
        recover_pending=True,
    )
    try:
        record = first_manager.submit(
            issue="wait for worker",
            repo_path=tmp_path,
            created_by="alice",
        )
    finally:
        first_manager.shutdown()

    second_manager = CodePilotTaskManager(
        max_workers=1,
        store=store,
        queue=InMemoryTaskQueue(),
        recover_pending=True,
    )
    try:
        restored = second_manager.get(record.task_id)
        assert restored is not None
        assert restored.status == TaskStatus.PENDING
    finally:
        second_manager.shutdown()


@pytest.mark.parametrize("read_method", ["get", "list", "task_metrics"])
def test_api_manager_observes_worker_updates(tmp_path: Path, read_method):
    database = tmp_path / "shared.sqlite3"
    queue = InMemoryTaskQueue()
    api = CodePilotTaskManager(store=SqliteApprovalSessionStore(database), queue=queue)
    worker = CodePilotTaskManager(store=SqliteApprovalSessionStore(database), queue=queue)
    try:
        task = api.submit(issue="inspect", repo_path=tmp_path, created_by="alice")
        assert worker.claim_queued(task.task_id)
        assert worker.fail_queued(task.task_id, "setup failed")
        if read_method == "list":
            observed = api.list()[0]
        else:
            observed = getattr(api, read_method)(task.task_id)
        assert observed.status == TaskStatus.FAILED
    finally:
        api.shutdown()
        worker.shutdown()


def test_starting_external_manager_does_not_fail_another_workers_task(tmp_path: Path):
    database = tmp_path / "active.sqlite3"
    queue = InMemoryTaskQueue()
    first = CodePilotTaskManager(store=SqliteApprovalSessionStore(database), queue=queue)
    second = None
    try:
        task = first.submit(issue="inspect", repo_path=tmp_path, created_by="alice")
        assert first.claim_queued(task.task_id)
        second = CodePilotTaskManager(store=SqliteApprovalSessionStore(database), queue=queue)
        assert second.get(task.task_id).status == TaskStatus.RUNNING
        assert SqliteApprovalSessionStore(database).get_task(task.task_id).status == "running"
        assert not second.claim_queued(task.task_id)
    finally:
        first.shutdown()
        if second is not None:
            second.shutdown()


@pytest.mark.parametrize("status", [
    TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.WAITING_APPROVAL,
])
def test_worker_acknowledges_redelivered_settled_task(tmp_path: Path, status):
    store = SqliteApprovalSessionStore(tmp_path / "settled.sqlite3")
    queue = InMemoryTaskQueue()
    manager = CodePilotTaskManager(store=store, queue=queue)
    try:
        task = manager.submit(issue="inspect", repo_path=tmp_path, created_by="alice")
        store.save_task(replace(store.get_task(task.task_id), status=status.value))
        worker = CodePilotWorker(queue=queue, task_manager=manager, agent_factory=lambda repo: None)
        assert worker.run_once(block_ms=0) == 0
        assert not queue._in_flight
        assert store.get_task(task.task_id).status == status.value
    finally:
        manager.shutdown()


def test_active_worker_keeps_live_state_for_queries_and_local_cancellation(tmp_path: Path):
    store = SqliteApprovalSessionStore(tmp_path / "live.sqlite3")
    queue = InMemoryTaskQueue()
    manager = CodePilotTaskManager(store=store, queue=queue)
    started, release = threading.Event(), threading.Event()

    class BlockingModel:
        def complete(self, request):
            started.set()
            assert release.wait(timeout=10)
            return ModelResponse(content="no-op")

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        task = manager.submit(issue="inspect", repo_path=tmp_path, created_by="alice")
        assert manager.claim_queued(task.task_id)
        live = manager.get(task.task_id)
        future = executor.submit(
            manager.execute_claimed, task.task_id,
            agent=CodePilotAgent(BlockingModel(), session_store=store),
        )
        assert started.wait(timeout=5)
        state = live.state
        assert state is not None
        assert manager.get(task.task_id) is live
        assert manager.list()[0] is live
        assert manager.list()[0].state is state
        manager.cancel(task.task_id, requested_by="alice")
        release.set()
        assert future.result(timeout=10)
        assert store.get_task(task.task_id).status == "cancelled"
        store.save_task(replace(store.get_task(task.task_id), final_message="external update"))
        assert manager.get(task.task_id).final_message == "external update"
    finally:
        release.set()
        executor.shutdown(wait=True)
        manager.shutdown()


class FakeRedis:
    def __init__(self) -> None:
        self.acked: list[str] = []

    def xgroup_create(self, *args: object, **kwargs: object) -> None:
        return None

    def xadd(self, stream: str, fields: dict[str, str]) -> bytes:
        return b"1-0"

    def xautoclaim(self, *args: object, **kwargs: object):
        return (b"0-0", [], [])

    def xreadgroup(self, *args: object, **kwargs: object):
        return [[b"codepilot:tasks", [(b"1-0", {b"task_id": b"task-1"})]]]

    def xack(self, stream: str, group: str, message_id: str) -> None:
        self.acked.append(message_id)


def test_redis_stream_adapter_reads_and_acknowledges_task_message():
    client = FakeRedis()
    queue = RedisTaskQueue("redis://unused", client=client)

    assert queue.enqueue("task-1") == "1-0"
    message = queue.read(block_ms=0)[0]
    assert message.task_id == "task-1"
    queue.ack(message.message_id)
    assert client.acked == ["1-0"]


def test_worker_keeps_message_pending_when_task_lookup_fails(tmp_path: Path, monkeypatch):
    store = SqliteApprovalSessionStore(tmp_path / "lookup.sqlite3")
    queue = InMemoryTaskQueue()
    manager = CodePilotTaskManager(store=store, queue=queue)
    record = manager.submit(issue="inspect", repo_path=tmp_path, created_by="alice")
    worker = CodePilotWorker(queue=queue, task_manager=manager, agent_factory=lambda repo: None)

    def unavailable(task_id):
        raise RuntimeError("temporary database failure")

    monkeypatch.setattr(manager, "get", unavailable)
    try:
        with pytest.raises(RuntimeError, match="temporary database failure"):
            worker.run_once(block_ms=0)
        assert [message.task_id for message in queue._in_flight.values()] == [record.task_id]
    finally:
        manager.shutdown()


def test_worker_keeps_running_task_message_pending(tmp_path: Path):
    store = SqliteApprovalSessionStore(tmp_path / "running.sqlite3")
    queue = InMemoryTaskQueue()
    manager = CodePilotTaskManager(store=store, queue=queue)
    record = manager.submit(issue="inspect", repo_path=tmp_path, created_by="alice")
    assert manager.claim_queued(record.task_id)
    worker = CodePilotWorker(queue=queue, task_manager=manager, agent_factory=lambda repo: None)
    try:
        assert worker.run_once(block_ms=0) == 0
        assert [message.task_id for message in queue._in_flight.values()] == [record.task_id]
    finally:
        manager.shutdown()
