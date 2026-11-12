"""CodePilot project package."""

from .agent import AgentRun, AgentSession, CodePilotAgent, ToolRegistryFactory
from .queue import InMemoryTaskQueue, RedisTaskQueue
from .session_store import ApprovalSessionRecord, SqliteApprovalSessionStore
from .state import CodePilotPhase, CodePilotState
from .task_manager import CodePilotTaskManager, TaskRecord, TaskStatus
from .tools import (
    PatchApplicationResult,
    SearchHit,
    TestRunResult,
    apply_patch,
    get_git_diff,
    read_file,
    run_tests,
    search_code,
)

__all__ = [
    "AgentRun",
    "AgentSession",
    "ApprovalSessionRecord",
    "CodePilotAgent",
    "CodePilotPhase",
    "CodePilotState",
    "CodePilotTaskManager",
    "InMemoryTaskQueue",
    "PatchApplicationResult",
    "RedisTaskQueue",
    "SearchHit",
    "SqliteApprovalSessionStore",
    "TaskRecord",
    "TaskStatus",
    "TestRunResult",
    "ToolRegistryFactory",
    "apply_patch",
    "get_git_diff",
    "read_file",
    "run_tests",
    "search_code",
]
