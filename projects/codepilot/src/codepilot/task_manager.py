"""In-process asynchronous task lifecycle for the CodePilot API."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .agent import AgentRun, CodePilotAgent
from .metrics import TaskMetrics, collect_trace, finish, summarize
from .metrics import snapshot as metrics_snapshot
from .queue import TaskQueue
from .session_store import (
    SqliteApprovalSessionStore,
    TaskRecordSnapshot,
    restore_state_snapshot,
    state_to_dict,
)
from .state import CodePilotPhase, CodePilotState


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    CANCELLING = "cancelling"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskRecord:
    task_id: str
    issue: str
    repo_path: Path
    created_by: str
    retry_count: int = 0
    parent_task_id: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    result: AgentRun | None = None
    error: str | None = None
    approval_session_id: str | None = None
    final_message: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    state: CodePilotState | None = field(default=None, repr=False)
    future: Future[None] | None = field(default=None, repr=False)
    metrics: TaskMetrics | None = field(default=None, repr=False)


class CodePilotTaskManager:
    """Bounded task runner with SQLite-backed task state and cooperative cancellation."""

    def __init__(
        self,
        *,
        max_workers: int = 2,
        store: SqliteApprovalSessionStore | None = None,
        queue: TaskQueue | None = None,
        recover_pending: bool | None = None,
        input_cost_per_million: float = 0.0,
        output_cost_per_million: float = 0.0,
    ) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if input_cost_per_million < 0 or output_cost_per_million < 0:
            raise ValueError("model token costs must not be negative")
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="codepilot")
        self._records: dict[str, TaskRecord] = {}
        self._owned_tasks: set[str] = set()
        self._lock = threading.RLock()
        self._store = store
        self._queue = queue
        self._recover_pending = queue is not None if recover_pending is None else recover_pending
        self._input_cost_per_million = input_cost_per_million
        self._output_cost_per_million = output_cost_per_million
        if store is not None:
            self._restore_persisted()

    @property
    def uses_external_queue(self) -> bool:
        return self._queue is not None

    def check_ready(self) -> tuple[bool, dict[str, str]]:
        """Return dependency readiness without exposing connection details."""

        checks = {"sqlite": "ok" if self._store is None or self._store.check_ready() else "error"}
        if self._queue is not None:
            check = getattr(self._queue, "check_ready", None)
            try:
                ready = bool(check()) if callable(check) else False
            except Exception:  # noqa: BLE001 - readiness must never crash the API
                ready = False
            checks["queue"] = "ok" if ready else "error"
        return all(value == "ok" for value in checks.values()), checks

    def submit(
        self,
        *,
        issue: str,
        repo_path: str | Path,
        created_by: str,
        agent: CodePilotAgent | None = None,
    ) -> TaskRecord:
        if not issue.strip():
            raise ValueError("issue must not be empty")
        if not created_by.strip():
            raise ValueError("created_by must not be empty")
        repo = Path(repo_path).resolve()
        if not repo.is_dir():
            raise NotADirectoryError(str(repo_path))
        record = TaskRecord(
            task_id=uuid.uuid4().hex,
            issue=issue,
            repo_path=repo,
            created_by=created_by,
        )
        record.metrics = TaskMetrics(record.task_id, record.status.value, record.created_at)
        with self._lock:
            self._persist(record)
            self._persist_metrics(record)
            self._records[record.task_id] = record
            self._emit_event(
                record,
                "task.created",
                actor=created_by,
                detail={"retry_count": record.retry_count},
            )
            if self._queue is not None:
                self._enqueue_or_fail(record)
            else:
                if agent is None:
                    raise ValueError("agent is required when an external queue is not configured")
                record.future = self._executor.submit(self._execute, record, agent)
        return record

    def retry(
        self,
        task_id: str,
        *,
        agent: CodePilotAgent | None = None,
        requested_by: str | None = None,
    ) -> TaskRecord:
        """Create a new attempt for a failed task while preserving its history."""

        with self._lock:
            record = self.get(task_id)
            if record is None:
                raise KeyError(f"task not found: {task_id}")
            if record.status != TaskStatus.FAILED:
                raise RuntimeError(f"only failed tasks can be retried: {task_id}")
            retry = TaskRecord(
                task_id=uuid.uuid4().hex,
                issue=record.issue,
                repo_path=record.repo_path,
                created_by=record.created_by,
                retry_count=record.retry_count + 1,
                parent_task_id=record.task_id,
            )
            retry.metrics = TaskMetrics(retry.task_id, retry.status.value, retry.created_at)
            self._persist(retry)
            self._persist_metrics(retry)
            self._records[retry.task_id] = retry
            self._emit_event(
                record,
                "task.retry.requested",
                actor=requested_by,
                detail={"child_task_id": retry.task_id, "retry_count": retry.retry_count},
            )
            self._emit_event(
                retry,
                "task.created",
                actor=requested_by or record.created_by,
                detail={"parent_task_id": record.task_id, "retry_count": retry.retry_count},
            )
            if self._queue is not None:
                self._enqueue_or_fail(retry)
            else:
                if agent is None:
                    raise ValueError("agent is required when an external queue is not configured")
                retry.future = self._executor.submit(self._execute, retry, agent)
            return retry

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            record = self._records.get(task_id)
            if self._store is None or (
                record is not None
                and (self._queue is None or task_id in self._owned_tasks)
            ):
                return record
            snapshot = self._store.get_task(task_id)
            if snapshot is None:
                return None
            record = self._record_from_snapshot(snapshot)
            self._hydrate_metrics(record)
            self._records[task_id] = record
            return record

    def list(self) -> list[TaskRecord]:
        """Return persisted task records in creation order."""

        with self._lock:
            if self._store is not None:
                for snapshot in self._store.list_tasks():
                    if snapshot.task_id not in self._records or (
                        self._queue is not None and snapshot.task_id not in self._owned_tasks
                    ):
                        record = self._record_from_snapshot(snapshot)
                        self._hydrate_metrics(record)
                        self._records[snapshot.task_id] = record
            return sorted(self._records.values(), key=lambda record: (record.created_at, record.task_id))

    def task_metrics(self, task_id: str) -> TaskMetrics | None:
        """Return metrics for a task, loading them from SQLite when needed."""

        with self._lock:
            record = self.get(task_id)
            if record is None:
                return None
            if record.metrics is None and self._store is not None:
                persisted = self._store.get_task_metrics(task_id)
                if persisted is not None:
                    from .metrics import restore

                    record.metrics = restore(persisted)
            return record.metrics

    def metrics_summary(self, *, window_minutes: int = 60) -> dict[str, object]:
        with self._lock:
            records = [record for record in self.list() if record.metrics is not None]
            snapshots = [metrics_snapshot(record.metrics) for record in records if record.metrics is not None]
            queue_depth = 0
            if self._queue is not None:
                depth = getattr(self._queue, "depth", None)
                if callable(depth):
                    queue_depth = int(depth())
            running_workers = sum(record.status == TaskStatus.RUNNING for record in records)
            return summarize(
                snapshots,
                window_minutes=window_minutes,
                queue_depth=queue_depth,
                running_workers=running_workers,
            )

    def attempts(self, task_id: str) -> list[TaskRecord]:
        """Return the complete retry lineage containing one task."""

        records = {record.task_id: record for record in self.list()}
        target = records.get(task_id)
        if target is None:
            raise KeyError(f"task not found: {task_id}")

        root_id = target.task_id
        visited: set[str] = set()
        while root_id not in visited:
            visited.add(root_id)
            parent_id = records[root_id].parent_task_id
            if parent_id is None or parent_id not in records:
                break
            root_id = parent_id

        def belongs_to_lineage(candidate: TaskRecord) -> bool:
            current_id = candidate.task_id
            seen: set[str] = set()
            while current_id not in seen:
                if current_id == root_id:
                    return True
                seen.add(current_id)
                parent_id = records.get(current_id)
                if parent_id is None or parent_id.parent_task_id is None:
                    return False
                current_id = parent_id.parent_task_id
            return False

        return sorted(
            (record for record in records.values() if belongs_to_lineage(record)),
            key=lambda record: (record.retry_count, record.created_at, record.task_id),
        )

    def claim_queued(self, task_id: str) -> bool:
        """Atomically claim one pending task for a Redis worker."""

        if self._queue is None or self._store is None:
            raise RuntimeError("external queue and task store are required")
        with self._lock:
            record = self.get(task_id)
            if record is None or record.status != TaskStatus.PENDING:
                return False
            if not self._store.claim_task(task_id):
                return False
            self._owned_tasks.add(task_id)
            record.status = TaskStatus.RUNNING
            _touch(record)
            self._mark_started(record)
            self._persist(record)
            self._persist_metrics(record)
            self._emit_event(record, "task.started", actor="worker")
            return True

    def execute_queued(self, task_id: str, *, agent: CodePilotAgent) -> bool:
        """Execute a task already claimed by an external worker."""

        if not self.claim_queued(task_id):
            return False
        return self.execute_claimed(task_id, agent=agent)

    def execute_claimed(self, task_id: str, *, agent: CodePilotAgent) -> bool:
        """Run a task after the caller has successfully claimed it."""

        record = self.get(task_id)
        if record is None or record.status != TaskStatus.RUNNING:
            return False
        try:
            self._execute(record, agent, claimed=True)
        finally:
            with self._lock:
                self._owned_tasks.discard(task_id)
        return True

    def fail_queued(self, task_id: str, error: str) -> bool:
        """Settle a claimed task when worker-side setup fails before Agent.run."""

        with self._lock:
            record = self.get(task_id)
            if record is None or record.status != TaskStatus.RUNNING:
                return False
            record.error = error
            record.status = TaskStatus.FAILED
            self._finish_metrics(record)
            _touch(record)
            self._persist(record)
            self._persist_metrics(record)
            self._emit_event(record, "task.failed", actor="worker", detail={"error": error})
            self._owned_tasks.discard(task_id)
            return True

    def record_approval_result(
        self,
        session_id: str,
        result: AgentRun,
        *,
        resolved_by: str | None = None,
    ) -> TaskRecord | None:
        """Reflect an approval decision completed through the HTTP approval API."""

        with self._lock:
            for record in self.list():
                if record.approval_session_id != session_id:
                    continue
                record.result = result
                record.state = result.state
                record.status = _status_for(result.state)
                record.final_message = result.final_message
                _touch(record)
                self._collect_result_metrics(record, result)
                self._persist(record)
                self._persist_metrics(record)
                self._emit_event(
                    record,
                    "approval.resolved",
                    actor=resolved_by,
                    detail={"status": record.status.value, "phase": result.state.phase.value},
                )
                self._emit_event(
                    record,
                    _event_type_for_status(record.status),
                    actor="system",
                    detail={"phase": result.state.phase.value},
                )
                return record
        return None

    def cancel(self, task_id: str, *, requested_by: str) -> TaskRecord:
        with self._lock:
            record = self.get(task_id)
            if record is None:
                raise KeyError(f"task not found: {task_id}")
            if not requested_by.strip():
                raise ValueError("requested_by must not be empty")
            if record.status in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                raise RuntimeError(f"task has already finished: {task_id}")
            record.cancel_event.set()
            if record.state is not None:
                record.state.request_cancel()
            if record.result is not None and record.result.session is not None:
                record.result = record.result.session.cancel(resolved_by=requested_by)
                record.state = record.result.state
                record.status = _status_for(record.result.state)
                record.final_message = record.result.final_message
            elif record.status == TaskStatus.WAITING_APPROVAL and record.approval_session_id:
                if self._store is None:
                    raise RuntimeError("task store is required to cancel a waiting approval")
                self._store.cancel(record.approval_session_id, resolved_by=requested_by)
                state = record.state
                if state is None:
                    approval = self._store.get(record.approval_session_id)
                    if approval is None:
                        raise RuntimeError("approval session is unavailable")
                    state = restore_state_snapshot(
                        approval.state,
                        record.repo_path,
                        context=f"task {record.task_id}",
                    )
                state.cancel("run cancelled by task request")
                self._store.update_state(record.approval_session_id, state)
                record.state = state
                record.status = TaskStatus.CANCELLED
            elif record.future is not None and record.future.cancel():
                record.status = TaskStatus.CANCELLED
            else:
                record.status = TaskStatus.CANCELLING
            _touch(record)
            if record.status == TaskStatus.CANCELLED:
                self._finish_metrics(record)
            self._persist(record)
            self._persist_metrics(record)
            self._emit_event(
                record,
                "task.cancel.requested",
                actor=requested_by,
                detail={"status": record.status.value},
            )
            if record.status == TaskStatus.CANCELLED:
                self._emit_event(record, "task.cancelled", actor=requested_by)
            return record

    def shutdown(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _execute(
        self,
        record: TaskRecord,
        agent: CodePilotAgent,
        *,
        claimed: bool = False,
    ) -> None:
        with self._lock:
            if not claimed:
                if record.cancel_event.is_set():
                    record.status = TaskStatus.CANCELLED
                    _touch(record)
                    self._finish_metrics(record)
                    self._persist(record)
                    self._persist_metrics(record)
                    self._emit_event(record, "task.cancelled", actor="system")
                    return
                record.status = TaskStatus.RUNNING
                _touch(record)
                self._mark_started(record)
                self._persist(record)
                self._persist_metrics(record)
                self._emit_event(record, "task.started", actor="system")

        def attach_state(state: CodePilotState) -> None:
            with self._lock:
                record.state = state
                if record.cancel_event.is_set():
                    state.request_cancel()
                self._persist(record)

        try:
            result = agent.run(
                record.issue,
                record.repo_path,
                created_by=record.created_by,
                state_callback=attach_state,
            )
        except Exception as exc:  # noqa: BLE001 - task boundary must always settle
            with self._lock:
                record.error = str(exc)
                record.status = (
                    TaskStatus.CANCELLED
                    if record.cancel_event.is_set()
                    else TaskStatus.FAILED
                )
                _touch(record)
                self._finish_metrics(record)
                self._persist(record)
                self._persist_metrics(record)
                self._emit_event(
                    record,
                    "task.cancelled" if record.status == TaskStatus.CANCELLED else "task.failed",
                    actor="system",
                    detail={"error": str(exc)} if str(exc) else None,
                )
            return

        with self._lock:
            record.result = result
            record.state = result.state
            record.status = _status_for(result.state)
            record.approval_session_id = result.session_id or record.approval_session_id
            record.final_message = result.final_message
            _touch(record)
            self._collect_result_metrics(record, result)
            self._persist(record)
            self._persist_metrics(record)
            self._emit_event(
                record,
                _event_type_for_status(record.status),
                actor="system",
                detail={"phase": result.state.phase.value},
            )

    def _restore_persisted(self) -> None:
        assert self._store is not None
        with self._lock:
            for snapshot in self._store.list_tasks():
                record = self._record_from_snapshot(snapshot)
                self._hydrate_metrics(record)
                # A new queue consumer cannot infer that another worker has died.
                interrupted = self._queue is None and record.status in {
                    TaskStatus.RUNNING, TaskStatus.CANCELLING,
                }
                interrupted = interrupted or (
                    record.status == TaskStatus.PENDING and not self._recover_pending
                )
                if interrupted:
                    if record.state is not None:
                        record.state.fail("task interrupted by process restart")
                    record.error = "task interrupted by process restart"
                    record.status = TaskStatus.FAILED
                    _touch(record)
                    self._finish_metrics(record)
                    self._persist(record)
                    self._persist_metrics(record)
                    self._emit_event(
                        record,
                        "task.interrupted",
                        actor="system",
                        detail={"reason": "process_restart"},
                    )
                    self._emit_event(
                        record,
                        "task.failed",
                        actor="system",
                        detail={"error": record.error},
                    )
                self._records[record.task_id] = record

    def _enqueue_or_fail(self, record: TaskRecord) -> None:
        assert self._queue is not None
        try:
            self._queue.enqueue(record.task_id)
        except Exception as exc:
            record.error = str(exc)
            record.status = TaskStatus.FAILED
            _touch(record)
            self._finish_metrics(record)
            self._persist(record)
            self._persist_metrics(record)
            self._emit_event(
                record,
                "task.failed",
                actor="system",
                detail={"error": str(exc), "reason": "queue_enqueue_failed"},
            )
            raise RuntimeError(f"could not enqueue task: {exc}") from exc

    def _emit_event(
        self,
        record: TaskRecord,
        event_type: str,
        *,
        actor: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        if self._store is None:
            return
        self._store.append_task_event(
            event_id=uuid.uuid4().hex,
            task_id=record.task_id,
            event_type=event_type,
            actor=actor,
            detail=detail,
        )

    @staticmethod
    def _record_from_snapshot(snapshot: TaskRecordSnapshot) -> TaskRecord:
        state = (
            restore_state_snapshot(
                snapshot.state,
                snapshot.repo_path,
                context=f"task {snapshot.task_id}",
            )
            if snapshot.state is not None
            else None
        )
        return TaskRecord(
            task_id=snapshot.task_id,
            issue=snapshot.issue,
            repo_path=Path(snapshot.repo_path),
            created_by=snapshot.created_by,
            retry_count=snapshot.retry_count,
            parent_task_id=snapshot.parent_task_id,
            status=TaskStatus(snapshot.status),
            state=state,
            error=snapshot.error,
            approval_session_id=snapshot.approval_session_id,
            final_message=snapshot.final_message,
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
            metrics=None,
        )

    def _persist(self, record: TaskRecord) -> None:
        if self._store is None:
            return
        self._store.save_task(
            TaskRecordSnapshot(
                task_id=record.task_id,
                issue=record.issue,
                repo_path=str(record.repo_path),
                created_by=record.created_by,
                retry_count=record.retry_count,
                parent_task_id=record.parent_task_id,
                status=record.status.value,
                state=state_to_dict(record.state) if record.state is not None else None,
                error=record.error,
                approval_session_id=record.approval_session_id,
                final_message=record.final_message,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
        )

    def _persist_metrics(self, record: TaskRecord) -> None:
        if self._store is not None and record.metrics is not None:
            self._store.save_task_metrics(metrics_snapshot(record.metrics))

    def _hydrate_metrics(self, record: TaskRecord) -> None:
        if self._store is None:
            return
        persisted = self._store.get_task_metrics(record.task_id)
        if persisted is not None:
            from .metrics import restore

            record.metrics = restore(persisted)

    def _ensure_metrics(self, record: TaskRecord) -> TaskMetrics:
        if record.metrics is None:
            persisted = self._store.get_task_metrics(record.task_id) if self._store is not None else None
            if persisted is not None:
                from .metrics import restore

                record.metrics = restore(persisted)
            else:
                record.metrics = TaskMetrics(record.task_id, record.status.value, record.created_at)
        return record.metrics

    def _mark_started(self, record: TaskRecord) -> None:
        metrics = self._ensure_metrics(record)
        if metrics.started_at is None:
            metrics.started_at = record.updated_at
            metrics.queue_wait_ms = max(
                0,
                int(
                    (datetime.fromisoformat(metrics.started_at) - datetime.fromisoformat(metrics.created_at))
                    .total_seconds()
                    * 1000
                ),
            )
        metrics.status = record.status.value

    def _collect_result_metrics(self, record: TaskRecord, result: AgentRun) -> None:
        metrics = self._ensure_metrics(record)
        collect_trace(metrics, result.trace_events)
        metrics.tests_passed = result.state.tests_passed
        metrics.estimated_cost_usd = round(
            metrics.input_tokens * self._input_cost_per_million / 1_000_000
            + metrics.output_tokens * self._output_cost_per_million / 1_000_000,
            6,
        )
        if record.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            finish(metrics, status=record.status.value, finished_at=record.updated_at)
        else:
            metrics.status = record.status.value

    def _finish_metrics(self, record: TaskRecord) -> None:
        metrics = self._ensure_metrics(record)
        finish(metrics, status=record.status.value, finished_at=record.updated_at)


def _status_for(state: CodePilotState) -> TaskStatus:
    if state.phase == CodePilotPhase.CANCELLED:
        return TaskStatus.CANCELLED
    if state.phase == CodePilotPhase.WAITING_APPROVAL:
        return TaskStatus.WAITING_APPROVAL
    if state.phase == CodePilotPhase.COMPLETED:
        return TaskStatus.COMPLETED
    if state.phase == CodePilotPhase.FAILED:
        return TaskStatus.FAILED
    return TaskStatus.RUNNING


def _event_type_for_status(status: TaskStatus) -> str:
    return {
        TaskStatus.WAITING_APPROVAL: "task.waiting_approval",
        TaskStatus.COMPLETED: "task.completed",
        TaskStatus.FAILED: "task.failed",
        TaskStatus.CANCELLED: "task.cancelled",
    }.get(status, "task.updated")


def _touch(record: TaskRecord) -> None:
    record.updated_at = datetime.now(UTC).isoformat()
