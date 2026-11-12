"""Production-shaped Uvicorn entry point for the CodePilot API."""

from __future__ import annotations

import os
from pathlib import Path

from agent_shared import Settings, TraceRecorder

from .agent import CodePilotAgent
from .api import ApiPrincipal, create_app
from .cli import build_model
from .queue import RedisTaskQueue
from .session_store import SqliteApprovalSessionStore
from .task_manager import CodePilotTaskManager


def _required_roots() -> list[Path]:
    raw = os.environ.get("CODEPILOT_ALLOWED_REPO_ROOTS", "")
    roots = [Path(value.strip()).resolve() for value in raw.split(";") if value.strip()]
    if not roots:
        raise ValueError("CODEPILOT_ALLOWED_REPO_ROOTS must contain at least one directory")
    return roots


def _required_api_keys() -> dict[str, ApiPrincipal]:
    raw = os.environ.get("CODEPILOT_API_KEYS", "")
    values: dict[str, ApiPrincipal] = {}
    for item in raw.split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError("CODEPILOT_API_KEYS entries must use key=subject|role format")
        key, identity = item.split("=", 1)
        parts = [part.strip() for part in identity.split("|") if part.strip()]
        if len(parts) < 2:
            raise ValueError("CODEPILOT_API_KEYS entries need a subject and at least one role")
        values[key.strip()] = ApiPrincipal(parts[0], frozenset(parts[1:]))
    if not values:
        raise ValueError("CODEPILOT_API_KEYS must contain at least one API key")
    return values


def build_app():
    settings = Settings.from_env()
    session_db = Path(
        os.environ.get("CODEPILOT_SESSION_DB", ".runtime/codepilot/approvals.sqlite3")
    )
    store = SqliteApprovalSessionStore(session_db)

    def agent_factory(repo: Path) -> CodePilotAgent:
        trace = TraceRecorder()
        trace.record("run.factory.created", project="codepilot", repo=str(repo))
        return CodePilotAgent(
            build_model(settings, repo, offline=False),
            trace=trace,
            session_store=store,
        )

    queue = None
    if os.environ.get("CODEPILOT_EXECUTION_MODE", "thread").casefold() == "redis":
        queue = RedisTaskQueue(
            os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            stream=os.environ.get("CODEPILOT_REDIS_STREAM", "codepilot:tasks"),
            group=os.environ.get("CODEPILOT_REDIS_GROUP", "codepilot-workers"),
        )
    task_manager = CodePilotTaskManager(
        store=store,
        queue=queue,
        input_cost_per_million=float(os.environ.get("CODEPILOT_INPUT_COST_PER_1M", "0")),
        output_cost_per_million=float(os.environ.get("CODEPILOT_OUTPUT_COST_PER_1M", "0")),
    )
    app = create_app(
        agent_factory,
        store,
        allowed_repo_roots=_required_roots(),
        api_keys=_required_api_keys(),
        task_manager=task_manager,
    )
    app.add_event_handler("shutdown", task_manager.shutdown)
    return app


app = build_app()
