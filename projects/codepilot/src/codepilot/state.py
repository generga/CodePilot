"""State contracts for the CodePilot workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class CodePilotPhase(StrEnum):
    PLANNING = "planning"
    SEARCHING = "searching"
    PATCH_READY = "patch_ready"
    WAITING_APPROVAL = "waiting_approval"
    TESTING = "testing"
    FIXING = "fixing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class CodePilotState:
    issue: str
    repo_path: Path
    phase: CodePilotPhase = CodePilotPhase.PLANNING
    plan: list[str] = field(default_factory=list)
    relevant_files: list[str] = field(default_factory=list)
    patch: str | None = None
    patch_paths: list[str] = field(default_factory=list)
    patch_applied: bool = False
    test_output: str | None = None
    tests_passed: bool | None = None
    errors: list[str] = field(default_factory=list)
    attempts: int = 0
    max_attempts: int = 3
    cancel_requested: bool = False

    def can_retry(self) -> bool:
        return self.attempts < self.max_attempts

    def fail(self, message: str) -> None:
        self.errors.append(message)
        self.phase = CodePilotPhase.FAILED

    def request_cancel(self) -> None:
        self.cancel_requested = True

    def cancel(self, message: str = "run cancelled") -> None:
        self.cancel_requested = True
        if message not in self.errors:
            self.errors.append(message)
        self.phase = CodePilotPhase.CANCELLED
