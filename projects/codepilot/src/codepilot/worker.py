"""Standalone worker process for Redis-backed CodePilot tasks."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path

from agent_shared import Settings, TraceRecorder

from .agent import CodePilotAgent
from .cli import build_model
from .queue import RedisTaskQueue, TaskQueue
from .session_store import SqliteApprovalSessionStore
from .task_manager import CodePilotTaskManager, TaskStatus

AgentFactory = Callable[[Path], CodePilotAgent]
LOGGER = logging.getLogger(__name__)


class CodePilotWorker:
    """Consume task IDs, claim them in SQLite, and execute them exactly once per claim."""

    def __init__(
        self,
        *,
        queue: TaskQueue,
        task_manager: CodePilotTaskManager,
        agent_factory: AgentFactory,
        max_setup_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
        max_retry_backoff_seconds: float = 30.0,
    ) -> None:
        if max_setup_retries < 0:
            raise ValueError("max_setup_retries must not be negative")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must not be negative")
        if max_retry_backoff_seconds < 0:
            raise ValueError("max_retry_backoff_seconds must not be negative")
        self.queue = queue
        self.task_manager = task_manager
        self.agent_factory = agent_factory
        self.max_setup_retries = max_setup_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_retry_backoff_seconds = max_retry_backoff_seconds

    def run_once(self, *, count: int = 1, block_ms: int = 1_000) -> int:
        processed = 0
        for message in self.queue.read(count=count, block_ms=block_ms):
            acknowledge = False
            try:
                record = self.task_manager.get(message.task_id)
                if record is None:
                    acknowledge = True
                    continue
                if not self.task_manager.claim_queued(message.task_id):
                    current = self.task_manager.get(message.task_id)
                    acknowledge = current is None or current.status in {
                        TaskStatus.COMPLETED,
                        TaskStatus.FAILED,
                        TaskStatus.CANCELLED,
                        TaskStatus.WAITING_APPROVAL,
                    }
                    continue
                try:
                    agent = self.agent_factory(record.repo_path)
                except Exception as exc:  # noqa: BLE001 - worker setup must settle the task
                    self.task_manager.fail_queued(message.task_id, str(exc))
                    acknowledge = True
                    if record.retry_count < self.max_setup_retries:
                        delay = min(
                            self.retry_backoff_seconds * (2**record.retry_count),
                            self.max_retry_backoff_seconds,
                        )
                        if delay:
                            time.sleep(delay)
                        try:
                            self.task_manager.retry(
                                message.task_id,
                                requested_by="worker:auto-retry",
                            )
                        except Exception:
                            LOGGER.exception(
                                "automatic setup retry could not be queued",
                                extra={"task_id": message.task_id},
                            )
                    continue
                self.task_manager.execute_claimed(message.task_id, agent=agent)
                current = self.task_manager.get(message.task_id)
                acknowledge = current is not None and current.status in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                    TaskStatus.WAITING_APPROVAL,
                }
                processed += 1
            finally:
                if acknowledge:
                    self.queue.ack(message.message_id)
        return processed

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            self.run_once()


def build_worker() -> CodePilotWorker:
    settings = Settings.from_env()
    store = SqliteApprovalSessionStore(
        os.environ.get("CODEPILOT_SESSION_DB", ".runtime/codepilot/approvals.sqlite3")
    )
    queue = RedisTaskQueue(
        os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        stream=os.environ.get("CODEPILOT_REDIS_STREAM", "codepilot:tasks"),
        group=os.environ.get("CODEPILOT_REDIS_GROUP", "codepilot-workers"),
        consumer=os.environ.get("CODEPILOT_WORKER_NAME"),
    )
    manager = CodePilotTaskManager(
        store=store,
        queue=queue,
        recover_pending=True,
        input_cost_per_million=float(os.environ.get("CODEPILOT_INPUT_COST_PER_1M", "0")),
        output_cost_per_million=float(os.environ.get("CODEPILOT_OUTPUT_COST_PER_1M", "0")),
    )

    def agent_factory(repo: Path) -> CodePilotAgent:
        trace = TraceRecorder()
        trace.record("worker.agent.created", project="codepilot", repo=str(repo))
        return CodePilotAgent(
            build_model(settings, repo, offline=False),
            trace=trace,
            session_store=store,
        )

    return CodePilotWorker(
        queue=queue,
        task_manager=manager,
        agent_factory=agent_factory,
        max_setup_retries=int(os.environ.get("CODEPILOT_WORKER_MAX_SETUP_RETRIES", "2")),
        retry_backoff_seconds=float(
            os.environ.get("CODEPILOT_WORKER_RETRY_BACKOFF_SECONDS", "1")
        ),
        max_retry_backoff_seconds=float(
            os.environ.get("CODEPILOT_WORKER_MAX_RETRY_BACKOFF_SECONDS", "30")
        ),
    )


def main() -> int:
    worker = build_worker()
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        worker.run_forever(stop_event)
    finally:
        worker.task_manager.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
