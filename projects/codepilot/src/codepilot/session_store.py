"""Durable storage for CodePilot approval sessions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .metrics import TaskMetricsSnapshot
from .state import CodePilotPhase, CodePilotState

_PENDING = "pending"
_APPROVED = "approved"
_REJECTED = "rejected"
_FAILED = "failed"
_CANCELLED = "cancelled"


class IdempotencyConflictError(RuntimeError):
    """The same idempotency key was reused with different request data."""


class IdempotencyInProgressError(RuntimeError):
    """An equivalent request is currently being handled by another worker."""


@dataclass(frozen=True)
class ApprovalSessionRecord:
    """Serialized continuation data kept independently of a Python process."""

    session_id: str
    repo_path: str
    state: dict[str, Any]
    messages: list[dict[str, Any]]
    next_step: int
    tool_message_index: int
    status: str
    created_by: str | None
    resolved_by: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TaskRecordSnapshot:
    """Serializable task state shared by the task manager and SQLite store."""

    task_id: str
    issue: str
    repo_path: str
    created_by: str
    retry_count: int
    parent_task_id: str | None
    status: str
    state: dict[str, Any] | None
    error: str | None
    approval_session_id: str | None
    final_message: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TaskEventRecord:
    """Immutable audit event for one asynchronous task."""

    event_id: str
    task_id: str
    event_type: str
    actor: str | None
    detail: dict[str, Any]
    created_at: str


class SqliteApprovalSessionStore:
    """SQLite-backed approval session repository with atomic decisions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create(
        self,
        *,
        session_id: str,
        state: CodePilotState,
        messages: list[dict[str, Any]],
        next_step: int,
        tool_message_index: int,
        created_by: str | None = None,
    ) -> None:
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        if next_step <= 0:
            raise ValueError("next_step must be positive")
        if not 0 <= tool_message_index < len(messages):
            raise ValueError("tool_message_index must point to a message")
        now = _timestamp()
        state_snapshot = state_to_dict(state)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO approval_sessions (
                        session_id, repo_path, state_json, messages_json,
                        next_step, tool_message_index, status, created_by,
                        resolved_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        str(state.repo_path),
                        json.dumps(state_snapshot, ensure_ascii=False),
                        json.dumps(messages, ensure_ascii=False),
                        next_step,
                        tool_message_index,
                        _PENDING,
                        _optional_actor(created_by),
                        None,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(f"approval session already exists: {session_id}") from exc
        except sqlite3.Error as exc:
            raise RuntimeError(f"could not persist approval session: {exc}") from exc

    def get(self, session_id: str) -> ApprovalSessionRecord | None:
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT session_id, repo_path, state_json, messages_json,
                           next_step, tool_message_index, status, created_by,
                           resolved_by, created_at, updated_at
                    FROM approval_sessions
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError(f"could not read approval session: {exc}") from exc
        if row is None:
            return None
        try:
            state = json.loads(row[2])
            messages = json.loads(row[3])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"approval session contains invalid JSON: {session_id}") from exc
        if not isinstance(state, dict) or not isinstance(messages, list):
            raise TypeError(f"approval session has invalid payload: {session_id}")
        if not all(isinstance(message, dict) for message in messages):
            raise TypeError(f"approval session has invalid messages: {session_id}")
        return ApprovalSessionRecord(
            session_id=row[0],
            repo_path=row[1],
            state=state,
            messages=messages,
            next_step=row[4],
            tool_message_index=row[5],
            status=row[6],
            created_by=row[7],
            resolved_by=row[8],
            created_at=row[9],
            updated_at=row[10],
        )

    def check_ready(self) -> bool:
        """Verify that SQLite accepts a lightweight read query."""

        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    def save_task(self, snapshot: TaskRecordSnapshot) -> None:
        """Insert or update one task record atomically."""

        if not snapshot.task_id.strip():
            raise ValueError("task_id must not be empty")
        if not snapshot.issue.strip():
            raise ValueError("task issue must not be empty")
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO tasks (
                        task_id, issue, repo_path, created_by, retry_count, parent_task_id,
                        status, state_json, error, approval_session_id, final_message,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        issue = excluded.issue,
                        repo_path = excluded.repo_path,
                        created_by = excluded.created_by,
                        retry_count = excluded.retry_count,
                        parent_task_id = excluded.parent_task_id,
                        status = excluded.status,
                        state_json = excluded.state_json,
                        error = excluded.error,
                        approval_session_id = excluded.approval_session_id,
                        final_message = excluded.final_message,
                        created_at = excluded.created_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        snapshot.task_id,
                        snapshot.issue,
                        snapshot.repo_path,
                        snapshot.created_by,
                        snapshot.retry_count,
                        snapshot.parent_task_id,
                        snapshot.status,
                        json.dumps(snapshot.state, ensure_ascii=False)
                        if snapshot.state is not None
                        else None,
                        snapshot.error,
                        snapshot.approval_session_id,
                        snapshot.final_message,
                        snapshot.created_at,
                        snapshot.updated_at,
                    ),
                )
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise RuntimeError(f"could not persist task: {exc}") from exc

    def get_task(self, task_id: str) -> TaskRecordSnapshot | None:
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT task_id, issue, repo_path, created_by, retry_count, parent_task_id,
                           status, state_json, error, approval_session_id,
                           final_message, created_at, updated_at
                    FROM tasks
                    WHERE task_id = ?
                    """,
                    (task_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError(f"could not read task: {exc}") from exc
        return _task_snapshot(row) if row is not None else None

    def claim_task(self, task_id: str) -> bool:
        """Atomically claim a pending task for one external worker."""

        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'running', updated_at = ?
                    WHERE task_id = ? AND status = 'pending'
                    """,
                    (_timestamp(), task_id),
                )
        except sqlite3.Error as exc:
            raise RuntimeError(f"could not claim task: {exc}") from exc
        return cursor.rowcount == 1

    def list_tasks(self) -> list[TaskRecordSnapshot]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT task_id, issue, repo_path, created_by, retry_count, parent_task_id,
                           status, state_json, error, approval_session_id,
                           final_message, created_at, updated_at
                    FROM tasks
                    ORDER BY created_at, task_id
                    """
                ).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError(f"could not list tasks: {exc}") from exc
        return [_task_snapshot(row) for row in rows]

    def save_task_metrics(self, metrics: TaskMetricsSnapshot) -> None:
        """Insert or update the durable runtime counters for one task."""

        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO task_metrics (
                        task_id, status, created_at, started_at, finished_at,
                        queue_wait_ms, execution_duration_ms, model_calls,
                        input_tokens, output_tokens, tool_calls, test_runs,
                        tests_passed, estimated_cost_usd
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        status = excluded.status,
                        created_at = excluded.created_at,
                        started_at = excluded.started_at,
                        finished_at = excluded.finished_at,
                        queue_wait_ms = excluded.queue_wait_ms,
                        execution_duration_ms = excluded.execution_duration_ms,
                        model_calls = excluded.model_calls,
                        input_tokens = excluded.input_tokens,
                        output_tokens = excluded.output_tokens,
                        tool_calls = excluded.tool_calls,
                        test_runs = excluded.test_runs,
                        tests_passed = excluded.tests_passed,
                        estimated_cost_usd = excluded.estimated_cost_usd
                    """,
                    _metrics_values(metrics),
                )
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise RuntimeError(f"could not persist task metrics: {exc}") from exc

    def get_task_metrics(self, task_id: str) -> TaskMetricsSnapshot | None:
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT task_id, status, created_at, started_at, finished_at,
                           queue_wait_ms, execution_duration_ms, model_calls,
                           input_tokens, output_tokens, tool_calls, test_runs,
                           tests_passed, estimated_cost_usd
                    FROM task_metrics
                    WHERE task_id = ?
                    """,
                    (task_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError(f"could not read task metrics: {exc}") from exc
        return _task_metrics(row) if row is not None else None

    def list_task_metrics(self) -> list[TaskMetricsSnapshot]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT task_id, status, created_at, started_at, finished_at,
                           queue_wait_ms, execution_duration_ms, model_calls,
                           input_tokens, output_tokens, tool_calls, test_runs,
                           tests_passed, estimated_cost_usd
                    FROM task_metrics
                    ORDER BY created_at, task_id
                    """
                ).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError(f"could not list task metrics: {exc}") from exc
        return [_task_metrics(row) for row in rows]

    def append_task_event(
        self,
        *,
        event_id: str,
        task_id: str,
        event_type: str,
        actor: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> TaskEventRecord:
        """Append one immutable task audit event."""

        if not event_id.strip():
            raise ValueError("event_id must not be empty")
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        if not event_type.strip():
            raise ValueError("event_type must not be empty")
        if actor is not None and not actor.strip():
            raise ValueError("actor must not be empty")
        payload = detail if detail is not None else {}
        if not isinstance(payload, dict):
            raise TypeError("event detail must be an object")
        event = TaskEventRecord(
            event_id=event_id,
            task_id=task_id,
            event_type=event_type,
            actor=actor,
            detail=dict(payload),
            created_at=_timestamp(),
        )
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO task_events (
                        event_id, task_id, event_type, actor, detail_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.task_id,
                        event.event_type,
                        event.actor,
                        json.dumps(event.detail, ensure_ascii=False),
                        event.created_at,
                    ),
                )
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise RuntimeError(f"could not append task event: {exc}") from exc
        return event

    def list_task_events(self, task_id: str, *, limit: int = 200) -> list[TaskEventRecord]:
        """Return the newest task events in chronological order."""

        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        if limit <= 0:
            raise ValueError("event limit must be positive")
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT event_id, task_id, event_type, actor, detail_json, created_at
                    FROM task_events
                    WHERE task_id = ?
                    ORDER BY created_at DESC, event_id DESC
                    LIMIT ?
                    """,
                    (task_id, limit),
                ).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError(f"could not list task events: {exc}") from exc
        events = [_task_event(row) for row in rows]
        return list(reversed(events))

    def resolve(
        self,
        session_id: str,
        approved: bool,
        *,
        resolved_by: str | None = None,
    ) -> None:
        """Atomically claim a pending session for one approval decision."""

        self._resolve_pending(
            session_id,
            _APPROVED if approved else _REJECTED,
            resolved_by=resolved_by,
        )

    def cancel(self, session_id: str, *, resolved_by: str | None = None) -> None:
        """Atomically cancel a pending session without treating it as rejection."""

        self._resolve_pending(session_id, _CANCELLED, resolved_by=resolved_by)

    def update_state(self, session_id: str, state: CodePilotState) -> None:
        """Persist the latest in-memory state after a decision has been claimed."""

        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE approval_sessions
                    SET state_json = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (json.dumps(state_to_dict(state), ensure_ascii=False), _timestamp(), session_id),
                )
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise RuntimeError(f"could not update approval session state: {exc}") from exc
        if cursor.rowcount != 1:
            raise KeyError(f"approval session not found: {session_id}")

    def _resolve_pending(
        self,
        session_id: str,
        status: str,
        *,
        resolved_by: str | None,
    ) -> None:
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE approval_sessions
                    SET status = ?, resolved_by = ?, updated_at = ?
                    WHERE session_id = ? AND status = ?
                    """,
                    (status, _optional_actor(resolved_by), _timestamp(), session_id, _PENDING),
                )
                if cursor.rowcount == 1:
                    return
                exists = connection.execute(
                    "SELECT 1 FROM approval_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError(f"could not resolve approval session: {exc}") from exc
        if exists is None:
            raise KeyError(f"approval session not found: {session_id}")
        raise RuntimeError(f"approval session has already been resolved: {session_id}")

    def mark_failed(self, session_id: str) -> None:
        """Record that an approved continuation could not apply its patch."""

        self._update_status(session_id, _FAILED)

    def begin_idempotent(
        self,
        *,
        scope: str,
        key: str,
        principal: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        """Reserve a request, or return the response from an earlier attempt."""

        for value, name in (
            (scope, "scope"),
            (key, "idempotency key"),
            (principal, "principal"),
            (request_hash, "request hash"),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        now = _timestamp()
        try:
            with self._connect() as connection:
                try:
                    connection.execute(
                        """
                        INSERT INTO idempotency_records (
                            scope, idempotency_key, principal, request_hash,
                            response_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, NULL, ?, ?)
                        """,
                        (scope, key, principal, request_hash, now, now),
                    )
                    return None
                except sqlite3.IntegrityError:
                    row = connection.execute(
                        """
                        SELECT principal, request_hash, response_json
                        FROM idempotency_records
                        WHERE scope = ? AND idempotency_key = ?
                        """,
                        (scope, key),
                    ).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError(f"could not reserve idempotency key: {exc}") from exc
        if row is None:
            raise RuntimeError("idempotency key disappeared during reservation")
        if row[0] != principal or row[1] != request_hash:
            raise IdempotencyConflictError("idempotency key was reused with different request data")
        if row[2] is None:
            raise IdempotencyInProgressError("an equivalent request is already in progress")
        try:
            response = json.loads(row[2])
        except json.JSONDecodeError as exc:
            raise RuntimeError("idempotency record contains invalid JSON") from exc
        if not isinstance(response, dict):
            raise TypeError("idempotency response must be a JSON object")
        return response

    def finish_idempotent(
        self,
        *,
        scope: str,
        key: str,
        response: dict[str, Any],
    ) -> None:
        """Persist the response that future retries must receive."""

        try:
            encoded = json.dumps(response, ensure_ascii=False)
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE idempotency_records
                    SET response_json = ?, updated_at = ?
                    WHERE scope = ? AND idempotency_key = ? AND response_json IS NULL
                    """,
                    (encoded, _timestamp(), scope, key),
                )
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise RuntimeError(f"could not persist idempotent response: {exc}") from exc
        if cursor.rowcount != 1:
            raise RuntimeError("idempotency record was not pending")

    def release_idempotent(self, *, scope: str, key: str) -> None:
        """Release a reservation when the original request failed before a response."""

        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    DELETE FROM idempotency_records
                    WHERE scope = ? AND idempotency_key = ? AND response_json IS NULL
                    """,
                    (scope, key),
                )
        except sqlite3.Error as exc:
            raise RuntimeError(f"could not release idempotency key: {exc}") from exc

    @staticmethod
    def restore_state(record: ApprovalSessionRecord) -> CodePilotState:
        """Rebuild a mutable workflow state from a persisted session record."""

        state = restore_state_snapshot(
            record.state,
            record.repo_path,
            context=f"approval session {record.session_id}",
        )
        if state.phase != CodePilotPhase.WAITING_APPROVAL:
            raise RuntimeError(f"approval session is not waiting for approval: {record.session_id}")
        return state

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS approval_sessions (
                        session_id TEXT PRIMARY KEY,
                        repo_path TEXT NOT NULL,
                        state_json TEXT NOT NULL,
                        messages_json TEXT NOT NULL,
                        next_step INTEGER NOT NULL CHECK (next_step > 0),
                        tool_message_index INTEGER NOT NULL CHECK (tool_message_index >= 0),
                        status TEXT NOT NULL CHECK (
                            status IN ('pending', 'approved', 'rejected', 'failed', 'cancelled')
                        ),
                        created_by TEXT,
                        resolved_by TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                _ensure_column(connection, "approval_sessions", "created_by", "TEXT")
                _ensure_column(connection, "approval_sessions", "resolved_by", "TEXT")
                _upgrade_approval_status_constraint(connection)
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS idempotency_records (
                        scope TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        principal TEXT NOT NULL,
                        request_hash TEXT NOT NULL,
                        response_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (scope, idempotency_key)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id TEXT PRIMARY KEY,
                        issue TEXT NOT NULL,
                        repo_path TEXT NOT NULL,
                        created_by TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN (
                                'pending', 'running', 'cancelling', 'waiting_approval',
                                'completed', 'failed', 'cancelled'
                            )
                        ),
                        retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
                        parent_task_id TEXT,
                        state_json TEXT,
                        error TEXT,
                        approval_session_id TEXT,
                        final_message TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                _ensure_column(
                    connection,
                    "tasks",
                    "retry_count",
                    "INTEGER NOT NULL DEFAULT 0",
                )
                _ensure_column(connection, "tasks", "parent_task_id", "TEXT")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS task_events (
                        event_id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        actor TEXT,
                        detail_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_task_events_task_time
                    ON task_events (task_id, created_at, event_id)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS task_metrics (
                        task_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        queue_wait_ms INTEGER,
                        execution_duration_ms INTEGER,
                        model_calls INTEGER NOT NULL DEFAULT 0,
                        input_tokens INTEGER NOT NULL DEFAULT 0,
                        output_tokens INTEGER NOT NULL DEFAULT 0,
                        tool_calls INTEGER NOT NULL DEFAULT 0,
                        test_runs INTEGER NOT NULL DEFAULT 0,
                        tests_passed INTEGER,
                        estimated_cost_usd REAL NOT NULL DEFAULT 0
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_task_metrics_created
                    ON task_metrics (created_at)
                    """
                )
        except sqlite3.Error as exc:
            raise RuntimeError(f"could not initialize approval session store: {exc}") from exc

    def _update_status(self, session_id: str, status: str) -> None:
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE approval_sessions
                    SET status = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (status, _timestamp(), session_id),
                )
        except sqlite3.Error as exc:
            raise RuntimeError(f"could not update approval session: {exc}") from exc
        if cursor.rowcount != 1:
            raise KeyError(f"approval session not found: {session_id}")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _optional_actor(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise ValueError("actor must not be empty")
    return value


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _upgrade_approval_status_constraint(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'approval_sessions'"
    ).fetchone()
    schema = (row[0] if row else "").casefold()
    if "'cancelled'" in schema:
        return

    connection.execute("ALTER TABLE approval_sessions RENAME TO approval_sessions_legacy")
    connection.execute(
        """
        CREATE TABLE approval_sessions (
            session_id TEXT PRIMARY KEY,
            repo_path TEXT NOT NULL,
            state_json TEXT NOT NULL,
            messages_json TEXT NOT NULL,
            next_step INTEGER NOT NULL CHECK (next_step > 0),
            tool_message_index INTEGER NOT NULL CHECK (tool_message_index >= 0),
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'approved', 'rejected', 'failed', 'cancelled')
            ),
            created_by TEXT,
            resolved_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO approval_sessions (
            session_id, repo_path, state_json, messages_json,
            next_step, tool_message_index, status, created_by,
            resolved_by, created_at, updated_at
        )
        SELECT session_id, repo_path, state_json, messages_json,
               next_step, tool_message_index, status, created_by,
               resolved_by, created_at, updated_at
        FROM approval_sessions_legacy
        """
    )
    connection.execute("DROP TABLE approval_sessions_legacy")


def state_to_dict(state: CodePilotState) -> dict[str, Any]:
    return {
        "issue": state.issue,
        "phase": state.phase.value,
        "plan": list(state.plan),
        "relevant_files": list(state.relevant_files),
        "patch": state.patch,
        "patch_paths": list(state.patch_paths),
        "patch_applied": state.patch_applied,
        "test_output": state.test_output,
        "tests_passed": state.tests_passed,
        "errors": list(state.errors),
        "attempts": state.attempts,
        "max_attempts": state.max_attempts,
        "cancel_requested": state.cancel_requested,
    }


def restore_state_snapshot(
    snapshot: dict[str, Any],
    repo_path: str | Path,
    *,
    context: str = "state",
) -> CodePilotState:
    """Rebuild a mutable workflow state from a validated JSON snapshot."""

    try:
        return CodePilotState(
            issue=_required_string(snapshot, "issue"),
            repo_path=Path(repo_path),
            phase=CodePilotPhase(_required_string(snapshot, "phase")),
            plan=_string_list(snapshot, "plan"),
            relevant_files=_string_list(snapshot, "relevant_files"),
            patch=_optional_string(snapshot, "patch"),
            patch_paths=_string_list(snapshot, "patch_paths"),
            patch_applied=_required_bool(snapshot, "patch_applied"),
            test_output=_optional_string(snapshot, "test_output"),
            tests_passed=_optional_bool(snapshot, "tests_passed"),
            errors=_string_list(snapshot, "errors"),
            attempts=_required_int(snapshot, "attempts"),
            max_attempts=_required_int(snapshot, "max_attempts"),
            cancel_requested=_optional_bool(snapshot, "cancel_requested") or False,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"{context} has invalid state") from exc


def _task_snapshot(row: sqlite3.Row) -> TaskRecordSnapshot:
    raw_state = row[7]
    if raw_state is None:
        state = None
    else:
        try:
            state = json.loads(raw_state)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"task contains invalid state JSON: {row[0]}") from exc
        if not isinstance(state, dict):
            raise TypeError(f"task state must be an object: {row[0]}")
    if not isinstance(row[10], str):
        raise TypeError(f"task final message must be a string: {row[0]}")
    return TaskRecordSnapshot(
        task_id=row[0],
        issue=row[1],
        repo_path=row[2],
        created_by=row[3],
        retry_count=row[4],
        parent_task_id=row[5],
        status=row[6],
        state=state,
        error=row[8],
        approval_session_id=row[9],
        final_message=row[10],
        created_at=row[11],
        updated_at=row[12],
    )


def _task_event(row: sqlite3.Row) -> TaskEventRecord:
    try:
        detail = json.loads(row[4])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"task event contains invalid detail JSON: {row[0]}") from exc
    if not isinstance(detail, dict):
        raise TypeError(f"task event detail must be an object: {row[0]}")
    return TaskEventRecord(
        event_id=row[0],
        task_id=row[1],
        event_type=row[2],
        actor=row[3],
        detail=detail,
        created_at=row[5],
    )


def _metrics_values(metrics: TaskMetricsSnapshot) -> tuple[Any, ...]:
    return (
        metrics.task_id,
        metrics.status,
        metrics.created_at,
        metrics.started_at,
        metrics.finished_at,
        metrics.queue_wait_ms,
        metrics.execution_duration_ms,
        metrics.model_calls,
        metrics.input_tokens,
        metrics.output_tokens,
        metrics.tool_calls,
        metrics.test_runs,
        None if metrics.tests_passed is None else int(metrics.tests_passed),
        metrics.estimated_cost_usd,
    )


def _task_metrics(row: sqlite3.Row) -> TaskMetricsSnapshot:
    return TaskMetricsSnapshot(
        task_id=row[0],
        status=row[1],
        created_at=row[2],
        started_at=row[3],
        finished_at=row[4],
        queue_wait_ms=row[5],
        execution_duration_ms=row[6],
        model_calls=row[7],
        input_tokens=row[8],
        output_tokens=row[9],
        tool_calls=row[10],
        test_runs=row[11],
        tests_passed=None if row[12] is None else bool(row[12]),
        estimated_cost_usd=float(row[13]),
    )


def _required_string(snapshot: dict[str, Any], key: str) -> str:
    value = snapshot[key]
    if not isinstance(value, str):
        raise TypeError(key)
    return value


def _optional_string(snapshot: dict[str, Any], key: str) -> str | None:
    value = snapshot[key]
    if value is not None and not isinstance(value, str):
        raise TypeError(key)
    return value


def _required_bool(snapshot: dict[str, Any], key: str) -> bool:
    value = snapshot[key]
    if not isinstance(value, bool):
        raise TypeError(key)
    return value


def _optional_bool(snapshot: dict[str, Any], key: str) -> bool | None:
    value = snapshot[key]
    if value is not None and not isinstance(value, bool):
        raise TypeError(key)
    return value


def _required_int(snapshot: dict[str, Any], key: str) -> int:
    value = snapshot[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(key)
    return value


def _string_list(snapshot: dict[str, Any], key: str) -> list[str]:
    value = snapshot[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(key)
    return list(value)
