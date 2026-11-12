"""HTTP API for the durable CodePilot approval workflow.

The API depends on FastAPI only when this optional module is imported. Model
construction stays outside the web layer so deployments can inject DeepSeek,
another OpenAI-compatible provider, or a test double.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import AgentRun, CodePilotAgent
from .session_store import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    SqliteApprovalSessionStore,
)
from .task_manager import CodePilotTaskManager, TaskStatus

AgentFactory = Callable[[Path], CodePilotAgent]
_RUN_ROLES = frozenset({"developer", "admin"})
_VIEW_ROLES = frozenset({"developer", "reviewer", "admin"})
_APPROVE_ROLES = frozenset({"reviewer", "admin"})


@dataclass(frozen=True)
class ApiPrincipal:
    """Identity associated with one configured API key."""

    subject: str
    roles: frozenset[str]

    def __post_init__(self) -> None:
        if not self.subject.strip():
            raise ValueError("principal subject must not be empty")
        if not self.roles:
            raise ValueError("principal must have at least one role")


class CreateRunRequest(BaseModel):
    issue: str = Field(min_length=1, max_length=4_000)
    repo_path: str = Field(min_length=1, max_length=1_024)


class ApprovalDecisionRequest(BaseModel):
    approved: bool


def create_app(
    agent_factory: AgentFactory,
    session_store: SqliteApprovalSessionStore,
    *,
    allowed_repo_roots: Sequence[str | Path],
    api_keys: Mapping[str, ApiPrincipal],
    task_manager: CodePilotTaskManager | None = None,
) -> FastAPI:
    """Build an API application with explicit repository isolation."""

    if not allowed_repo_roots:
        raise ValueError("allowed_repo_roots must not be empty")
    roots = tuple(Path(root).resolve() for root in allowed_repo_roots)
    if not all(root.is_dir() for root in roots):
        raise NotADirectoryError("every allowed repository root must be a directory")
    if not api_keys:
        raise ValueError("api_keys must not be empty")
    configured_keys = dict(api_keys)
    if any(not key.strip() for key in configured_keys):
        raise ValueError("API keys must not be empty")

    app = FastAPI(title="CodePilot API", version="0.1.0")
    web_root = Path(__file__).parents[2] / "web"
    if web_root.is_dir():
        app.mount("/console/assets", StaticFiles(directory=web_root), name="console-assets")

        @app.get("/console", include_in_schema=False)
        def console() -> FileResponse:
            return FileResponse(
                web_root / "index.html",
                headers={"Cache-Control": "no-store"},
            )

        @app.get("/", include_in_schema=False)
        def console_root() -> FileResponse:
            return FileResponse(
                web_root / "index.html",
                headers={"Cache-Control": "no-store"},
            )

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readiness() -> dict[str, Any]:
        if task_manager is None:
            return {"status": "ok", "checks": {"sqlite": "ok"}}
        ready, checks = task_manager.check_ready()
        payload = {"status": "ok" if ready else "degraded", "checks": checks}
        if not ready:
            from fastapi.responses import JSONResponse

            return JSONResponse(status_code=503, content=payload)
        return payload

    @app.post("/v1/runs", status_code=status.HTTP_200_OK)
    def create_run(
        request: CreateRunRequest,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        principal = _authenticate(x_api_key, configured_keys, _RUN_ROLES)
        key = _required_idempotency_key(idempotency_key)
        scope = "create-run"
        repo = _resolve_allowed_repo(request.repo_path, roots)
        request_hash = _request_hash(
            {"issue": request.issue, "repo_path": str(repo)}
        )
        cached = _begin_idempotent(session_store, scope, key, principal.subject, request_hash)
        if cached is not None:
            return cached
        try:
            result = agent_factory(repo).run(request.issue, repo, created_by=principal.subject)
        except (NotADirectoryError, ValueError) as exc:
            session_store.release_idempotent(scope=scope, key=key)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (KeyError, OSError, RuntimeError) as exc:
            session_store.release_idempotent(scope=scope, key=key)
            raise HTTPException(status_code=502, detail=f"agent execution failed: {exc}") from exc
        payload = _run_payload(result)
        session_store.finish_idempotent(scope=scope, key=key, response=payload)
        return payload

    @app.get("/v1/approval-sessions/{session_id}")
    def get_approval_session(
        session_id: str,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        _authenticate(x_api_key, configured_keys, _VIEW_ROLES)
        try:
            record = session_store.get(session_id)
        except (TypeError, ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail="approval session not found")
        return {
            "session_id": record.session_id,
            "status": record.status,
            "repo_path": record.repo_path,
            "issue": record.state.get("issue", ""),
            "phase": record.state.get("phase", ""),
            "relevant_files": record.state.get("relevant_files", []),
            "patch": record.state.get("patch"),
            "created_by": record.created_by,
            "resolved_by": record.resolved_by,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    @app.post("/v1/approval-sessions/{session_id}/decision")
    def decide_approval(
        session_id: str,
        request: ApprovalDecisionRequest,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        principal = _authenticate(x_api_key, configured_keys, _APPROVE_ROLES)
        key = _required_idempotency_key(idempotency_key)
        scope = f"approval-decision:{session_id}"
        request_hash = _request_hash({"approved": request.approved})
        cached = _begin_idempotent(session_store, scope, key, principal.subject, request_hash)
        if cached is not None:
            return cached
        record = session_store.get(session_id)
        if record is None:
            session_store.release_idempotent(scope=scope, key=key)
            raise HTTPException(status_code=404, detail="approval session not found")
        repo = _resolve_allowed_repo(record.repo_path, roots)
        try:
            result = agent_factory(repo).resume(session_id)
            if result.session is None:
                raise RuntimeError("restored run has no approval session")
            completed = result.session.approve(request.approved, resolved_by=principal.subject)
            if task_manager is not None:
                task_manager.record_approval_result(
                    session_id,
                    completed,
                    resolved_by=principal.subject,
                )
        except (KeyError, ValueError, RuntimeError) as exc:
            session_store.release_idempotent(scope=scope, key=key)
            message = str(exc)
            code = 409 if "resolved" in message or "already" in message else 400
            raise HTTPException(status_code=code, detail=message) from exc
        except (NotADirectoryError, OSError) as exc:
            session_store.release_idempotent(scope=scope, key=key)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload = _run_payload(completed)
        session_store.finish_idempotent(scope=scope, key=key, response=payload)
        return payload

    @app.post("/v1/tasks", status_code=status.HTTP_202_ACCEPTED)
    def create_task(
        request: CreateRunRequest,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        principal = _authenticate(x_api_key, configured_keys, _RUN_ROLES)
        if task_manager is None:
            raise HTTPException(status_code=503, detail="async task manager is not configured")
        key = _required_idempotency_key(idempotency_key)
        repo = _resolve_allowed_repo(request.repo_path, roots)
        scope = "create-task"
        request_hash = _request_hash({"issue": request.issue, "repo_path": str(repo)})
        cached = _begin_idempotent(session_store, scope, key, principal.subject, request_hash)
        if cached is not None:
            return cached
        try:
            agent = None if task_manager.uses_external_queue else agent_factory(repo)
            record = task_manager.submit(
                issue=request.issue,
                repo_path=repo,
                created_by=principal.subject,
                agent=agent,
            )
        except (NotADirectoryError, ValueError) as exc:
            session_store.release_idempotent(scope=scope, key=key)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            session_store.release_idempotent(scope=scope, key=key)
            code = 503 if "could not enqueue task" in str(exc) else 400
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        payload = _task_payload(record)
        session_store.finish_idempotent(scope=scope, key=key, response=payload)
        return payload

    @app.get("/v1/tasks/{task_id}")
    def get_task(
        task_id: str,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        principal = _authenticate(x_api_key, configured_keys, _VIEW_ROLES)
        if task_manager is None:
            raise HTTPException(status_code=503, detail="async task manager is not configured")
        record = task_manager.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="task not found")
        _authorize_task_view(principal, record)
        return _task_payload(record)

    @app.get("/v1/tasks/{task_id}/attempts")
    def get_task_attempts(
        task_id: str,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        principal = _authenticate(x_api_key, configured_keys, _VIEW_ROLES)
        if task_manager is None:
            raise HTTPException(status_code=503, detail="async task manager is not configured")
        record = task_manager.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="task not found")
        _authorize_task_view(principal, record)
        try:
            attempts = task_manager.attempts(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        return {
            "task_id": task_id,
            "root_task_id": attempts[0].task_id,
            "attempts": [_attempt_payload(attempt) for attempt in attempts],
            "count": len(attempts),
        }

    @app.get("/v1/tasks/{task_id}/events")
    def get_task_events(
        task_id: str,
        limit: int = Query(default=200, ge=1, le=500),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        principal = _authenticate(x_api_key, configured_keys, _VIEW_ROLES)
        if task_manager is None:
            raise HTTPException(status_code=503, detail="async task manager is not configured")
        record = task_manager.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="task not found")
        _authorize_task_view(principal, record)
        try:
            events = session_store.list_task_events(task_id, limit=limit)
        except (TypeError, ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"task_id": task_id, "events": [_event_payload(event) for event in events], "count": len(events)}

    @app.post("/v1/tasks/{task_id}/retry", status_code=status.HTTP_202_ACCEPTED)
    def retry_task(
        task_id: str,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        principal = _authenticate(x_api_key, configured_keys, _VIEW_ROLES)
        if task_manager is None:
            raise HTTPException(status_code=503, detail="async task manager is not configured")
        record = task_manager.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="task not found")
        if not _can_manage_task(principal, record):
            raise HTTPException(status_code=403, detail="only the task owner or reviewer can retry")
        if record.status != TaskStatus.FAILED:
            raise HTTPException(status_code=409, detail=f"only failed tasks can be retried: {task_id}")
        repo = _resolve_allowed_repo(str(record.repo_path), roots)
        key = _required_idempotency_key(idempotency_key)
        scope = f"retry-task:{task_id}"
        request_hash = _request_hash({"task_id": task_id, "action": "retry"})
        cached = _begin_idempotent(session_store, scope, key, principal.subject, request_hash)
        if cached is not None:
            return cached
        try:
            agent = None if task_manager.uses_external_queue else agent_factory(repo)
            retried = task_manager.retry(
                task_id,
                agent=agent,
                requested_by=principal.subject,
            )
        except KeyError as exc:
            session_store.release_idempotent(scope=scope, key=key)
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            session_store.release_idempotent(scope=scope, key=key)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            session_store.release_idempotent(scope=scope, key=key)
            message = str(exc)
            if "only failed" in message:
                code = 409
            elif "could not enqueue task" in message:
                code = 503
            else:
                code = 502
            raise HTTPException(status_code=code, detail=message) from exc
        except (NotADirectoryError, OSError) as exc:
            session_store.release_idempotent(scope=scope, key=key)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload = _task_payload(retried)
        session_store.finish_idempotent(scope=scope, key=key, response=payload)
        return payload

    @app.get("/v1/tasks")
    def list_tasks(
        limit: int = Query(default=50, ge=1, le=100),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        principal = _authenticate(x_api_key, configured_keys, _VIEW_ROLES)
        if task_manager is None:
            raise HTTPException(status_code=503, detail="async task manager is not configured")
        records = [record for record in task_manager.list() if _can_view_task(principal, record)]
        records = records[-limit:]
        return {"tasks": [_task_payload(record) for record in reversed(records)], "count": len(records)}

    @app.get("/v1/metrics")
    def get_metrics(
        window_minutes: int = Query(default=60, ge=1, le=10_080),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        _authenticate(x_api_key, configured_keys, _VIEW_ROLES)
        if task_manager is None:
            raise HTTPException(status_code=503, detail="async task manager is not configured")
        try:
            return task_manager.metrics_summary(window_minutes=window_minutes)
        except (TypeError, ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/v1/tasks/{task_id}/cancel")
    def cancel_task(
        task_id: str,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        principal = _authenticate(x_api_key, configured_keys, _VIEW_ROLES)
        if task_manager is None:
            raise HTTPException(status_code=503, detail="async task manager is not configured")
        record = task_manager.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="task not found")
        if not _can_manage_task(principal, record):
            raise HTTPException(status_code=403, detail="only the task owner or reviewer can cancel")
        key = _required_idempotency_key(idempotency_key)
        scope = f"cancel-task:{task_id}"
        request_hash = _request_hash({"task_id": task_id, "action": "cancel"})
        cached = _begin_idempotent(session_store, scope, key, principal.subject, request_hash)
        if cached is not None:
            return cached
        try:
            cancelled = task_manager.cancel(task_id, requested_by=principal.subject)
        except (KeyError, ValueError) as exc:
            session_store.release_idempotent(scope=scope, key=key)
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            session_store.release_idempotent(scope=scope, key=key)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        payload = _task_payload(cancelled)
        session_store.finish_idempotent(scope=scope, key=key, response=payload)
        return payload

    return app


def _authenticate(
    raw_key: str | None,
    api_keys: Mapping[str, ApiPrincipal],
    required_roles: frozenset[str],
) -> ApiPrincipal:
    if not raw_key or raw_key not in api_keys:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    principal = api_keys[raw_key]
    if not principal.roles.intersection(required_roles):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
    return principal


def _can_view_task(principal: ApiPrincipal, record: Any) -> bool:
    return bool(principal.roles.intersection(_APPROVE_ROLES)) or principal.subject == record.created_by


def _can_manage_task(principal: ApiPrincipal, record: Any) -> bool:
    return _can_view_task(principal, record)


def _authorize_task_view(principal: ApiPrincipal, record: Any) -> None:
    if not _can_view_task(principal, record):
        raise HTTPException(status_code=403, detail="only the task owner or reviewer can view this task")


def _required_idempotency_key(value: str | None) -> str:
    if value is None or not value.strip() or len(value.strip()) > 200:
        raise HTTPException(status_code=400, detail="a valid Idempotency-Key header is required")
    return value.strip()


def _request_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _begin_idempotent(
    store: SqliteApprovalSessionStore,
    scope: str,
    key: str,
    principal: str,
    request_hash: str,
) -> dict[str, Any] | None:
    try:
        return store.begin_idempotent(
            scope=scope,
            key=key,
            principal=principal,
            request_hash=request_hash,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _resolve_allowed_repo(raw_path: str, roots: Sequence[Path]) -> Path:
    repo = Path(raw_path).expanduser().resolve()
    if not repo.is_dir():
        raise HTTPException(status_code=404, detail="repository directory not found")
    if not any(_is_relative_to(repo, root) for root in roots):
        raise HTTPException(status_code=403, detail="repository is outside the allowed roots")
    return repo


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _run_payload(result: AgentRun) -> dict[str, Any]:
    state = result.state
    return {
        "status": state.phase.value,
        "session_id": result.session_id,
        "approval_required": result.approval_required,
        "repo_path": str(state.repo_path),
        "issue": state.issue,
        "relevant_files": list(state.relevant_files),
        "patch": state.patch if not state.patch_applied else None,
        "patch_applied": state.patch_applied,
        "tests_passed": state.tests_passed,
        "test_output": state.test_output,
        "errors": list(state.errors),
        "final_message": result.final_message,
    }


def _task_payload(record: Any) -> dict[str, Any]:
    result = record.result
    state = result.state if result is not None else record.state
    return {
        "task_id": record.task_id,
        "status": record.status.value,
        "issue": record.issue,
        "repo_path": str(record.repo_path),
        "created_by": record.created_by,
        "retry_count": record.retry_count,
        "parent_task_id": record.parent_task_id,
        "retryable": record.status.value == "failed",
        "session_id": record.approval_session_id
        or (result.session_id if result is not None else None),
        "phase": state.phase.value if state is not None else None,
        "relevant_files": list(state.relevant_files) if state is not None else [],
        "patch": state.patch if state is not None and not state.patch_applied else None,
        "cancel_requested": record.cancel_event.is_set(),
        "patch_applied": state.patch_applied if state is not None else False,
        "tests_passed": state.tests_passed if state is not None else None,
        "test_output": state.test_output if state is not None else None,
        "errors": list(state.errors) if state is not None else ([record.error] if record.error else []),
        "final_message": record.final_message,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _attempt_payload(record: Any) -> dict[str, Any]:
    return {
        "task_id": record.task_id,
        "issue": record.issue,
        "status": record.status.value,
        "created_by": record.created_by,
        "retry_count": record.retry_count,
        "parent_task_id": record.parent_task_id,
        "error": record.error,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _event_payload(event: Any) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "task_id": event.task_id,
        "event_type": event.event_type,
        "actor": event.actor,
        "detail": event.detail,
        "created_at": event.created_at,
    }
