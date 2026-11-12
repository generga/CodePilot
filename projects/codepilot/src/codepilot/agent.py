"""The first model-driven CodePilot loop.

The model can inspect the repository and propose a patch. Applying that patch
is an agent-controlled side effect guarded by an explicit approval callback.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_shared import (
    ModelClient,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    TraceRecorder,
)

from .session_store import SqliteApprovalSessionStore
from .state import CodePilotPhase, CodePilotState
from .tools import apply_patch, get_git_diff, read_file, run_tests, search_code

SYSTEM_PROMPT = """You are CodePilot, a cautious coding assistant.

Work only inside the provided repository. First inspect relevant code before
proposing a change. Use propose_patch with a standard unified diff and an
explicit list of relative paths. Never claim tests passed unless run_tests
returned a successful result. Keep changes narrowly scoped.
After a patch is applied, run the tests before proposing any additional patch.
"""


@dataclass(frozen=True)
class AgentRun:
    state: CodePilotState
    final_message: str = ""
    session: AgentSession | None = None
    trace_events: tuple[dict[str, Any], ...] = ()

    @property
    def approval_required(self) -> bool:
        return self.session is not None

    @property
    def session_id(self) -> str | None:
        return self.session.session_id if self.session is not None else None


ApprovalCallback = Callable[[CodePilotState], bool]
ToolRegistryFactory = Callable[[CodePilotState], ToolRegistry]


class AgentSession:
    """In-memory continuation for a run paused at patch approval."""

    def __init__(
        self,
        agent: CodePilotAgent,
        state: CodePilotState,
        messages: list[dict[str, Any]],
        registry: ToolRegistry,
        next_step: int,
        tool_message_index: int,
        session_id: str,
        session_store: SqliteApprovalSessionStore | None,
    ) -> None:
        self._agent = agent
        self._state = state
        self._messages = messages
        self._registry = registry
        self._next_step = next_step
        self._tool_message_index = tool_message_index
        self.session_id = session_id
        self._session_store = session_store
        self._closed = False

    def approve(self, approved: bool, *, resolved_by: str | None = None) -> AgentRun:
        """Approve or reject the pending patch and continue the same run."""

        if self._closed:
            raise RuntimeError("approval session has already been resolved")
        if self._session_store is not None:
            self._session_store.resolve(self.session_id, approved, resolved_by=resolved_by)
        self._closed = True
        if not approved:
            self._state.fail("patch rejected by approval session")
            if self._session_store is not None:
                self._session_store.update_state(self.session_id, self._state)
            self._agent.trace.record("approval.rejected", paths=self._state.relevant_files)
            return self._agent._complete(self._state)

        result = self._agent._apply_patch_to_state(self._state)
        self._agent.trace.record(
            "approval.granted",
            paths=self._state.relevant_files,
            ok=result.ok,
        )
        if not result.ok:
            if self._session_store is not None:
                self._session_store.mark_failed(self.session_id)
            self._state.fail(f"apply_patch: {result.error}")
            if self._session_store is not None:
                self._session_store.update_state(self.session_id, self._state)
            return self._agent._complete(self._state)

        self._messages[self._tool_message_index]["content"] = json.dumps(
            result.as_message(), ensure_ascii=False
        )
        completed = self._agent._continue(
            self._state,
            self._messages,
            self._registry,
            start_step=self._next_step,
        )
        if self._session_store is not None:
            self._session_store.update_state(self.session_id, completed.state)
        return completed

    def cancel(self, *, resolved_by: str | None = None) -> AgentRun:
        """Cancel a pending approval without applying its patch."""

        if self._closed:
            raise RuntimeError("approval session has already been resolved")
        if self._session_store is not None:
            self._session_store.cancel(self.session_id, resolved_by=resolved_by)
        self._closed = True
        self._state.cancel("run cancelled by approval session")
        if self._session_store is not None:
            self._session_store.update_state(self.session_id, self._state)
        self._agent.trace.record("approval.cancelled", paths=self._state.relevant_files)
        return self._agent._complete(self._state)


class CodePilotAgent:
    def __init__(
        self,
        model: ModelClient,
        *,
        max_steps: int = 20,
        approval_callback: ApprovalCallback | None = None,
        trace: TraceRecorder | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        tool_registry_factory: ToolRegistryFactory | None = None,
        session_store: SqliteApprovalSessionStore | None = None,
    ) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps must be positive")
        if not system_prompt.strip():
            raise ValueError("system_prompt must not be empty")
        self.model = model
        self.max_steps = max_steps
        self.approval_callback = approval_callback
        self.trace = trace or TraceRecorder()
        self.system_prompt = system_prompt
        self.tool_registry_factory = tool_registry_factory
        self.session_store = session_store

    def run(
        self,
        issue: str,
        repo_path: str | Path,
        *,
        created_by: str | None = None,
        state_callback: Callable[[CodePilotState], None] | None = None,
    ) -> AgentRun:
        if not issue.strip():
            raise ValueError("issue must not be empty")
        root = Path(repo_path).resolve()
        if not root.is_dir():
            raise NotADirectoryError(str(repo_path))

        state = CodePilotState(issue=issue, repo_path=root)
        if state_callback is not None:
            state_callback(state)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": f"Repository: {root}\nIssue: {issue}"},
        ]
        registry = self._create_tool_registry(state)
        return self._continue(state, messages, registry, start_step=1, created_by=created_by)

    def resume(self, session_id: str) -> AgentRun:
        """Restore a pending approval session after a process restart."""

        if self.session_store is None:
            raise RuntimeError("resume requires a SqliteApprovalSessionStore")
        record = self.session_store.get(session_id)
        if record is None:
            raise KeyError(f"approval session not found: {session_id}")
        if record.status != "pending":
            raise RuntimeError(f"approval session has already been resolved: {session_id}")
        root = Path(record.repo_path).resolve()
        if not root.is_dir():
            raise NotADirectoryError(record.repo_path)
        state = self.session_store.restore_state(record)
        state.repo_path = root
        registry = self._create_tool_registry(state)
        return AgentRun(
            state,
            session=AgentSession(
                self,
                state,
                record.messages,
                registry,
                record.next_step,
                record.tool_message_index,
                record.session_id,
                self.session_store,
            ),
            trace_events=tuple(self.trace.events()),
        )

    def _continue(
        self,
        state: CodePilotState,
        messages: list[dict[str, Any]],
        registry: ToolRegistry,
        *,
        start_step: int,
        created_by: str | None = None,
    ) -> AgentRun:
        final_message = ""
        for step in range(start_step, self.max_steps + 1):
            if self._cancel_if_requested(state):
                break
            self.trace.record("agent.step.started", step=step, phase=state.phase.value)
            response = self.model.complete(
                ModelRequest(messages=tuple(messages), tools=tuple(registry.schemas()))
            )
            messages.append(self._assistant_message(response))
            self.trace.record(
                "model.completed",
                step=step,
                tool_call_count=len(response.tool_calls),
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
            if self._cancel_if_requested(state):
                break

            if not response.tool_calls:
                final_message = response.content
                if state.patch_applied and state.tests_passed is True:
                    state.phase = CodePilotPhase.COMPLETED
                elif state.phase != CodePilotPhase.WAITING_APPROVAL:
                    state.fail("model stopped before a successful test result")
                break

            state.phase = CodePilotPhase.SEARCHING
            for tool_call in response.tool_calls:
                if self._cancel_if_requested(state):
                    break
                tool_result, should_pause = self._execute_tool(tool_call, registry, state)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    }
                )
                if should_pause:
                    session_id = uuid.uuid4().hex
                    if self.session_store is not None:
                        try:
                            self.session_store.create(
                                session_id=session_id,
                                state=state,
                                messages=messages,
                                next_step=step + 1,
                                tool_message_index=len(messages) - 1,
                                created_by=created_by,
                            )
                        except RuntimeError as exc:
                            state.fail(f"approval session persistence failed: {exc}")
                            self.trace.record("approval.persistence_failed", error=str(exc))
                            return self._complete(state)
                    self.trace.record(
                        "agent.paused",
                        reason="approval_required",
                        touched_paths=state.relevant_files,
                    )
                    return AgentRun(
                        state,
                        session=AgentSession(
                            self,
                            state,
                            messages,
                            registry,
                            step + 1,
                            len(messages) - 1,
                            session_id,
                            self.session_store,
                        ),
                        trace_events=tuple(self.trace.events()),
                    )
                if state.phase in {CodePilotPhase.FAILED, CodePilotPhase.CANCELLED}:
                    break
            if state.phase in {CodePilotPhase.FAILED, CodePilotPhase.CANCELLED}:
                break
        else:
            state.fail(f"agent exceeded max_steps={self.max_steps}")

        return self._complete(state, final_message)

    def _create_tool_registry(self, state: CodePilotState) -> ToolRegistry:
        if self.tool_registry_factory is not None:
            return self.tool_registry_factory(state)
        return self._build_tool_registry(state)

    def _complete(self, state: CodePilotState, final_message: str = "") -> AgentRun:
        self.trace.record("agent.run.completed", phase=state.phase.value, errors=state.errors)
        return AgentRun(state, final_message, trace_events=tuple(self.trace.events()))

    def _cancel_if_requested(self, state: CodePilotState) -> bool:
        if not state.cancel_requested:
            return False
        if state.phase != CodePilotPhase.CANCELLED:
            state.cancel()
        self.trace.record("agent.cancelled", phase=state.phase.value)
        return True

    def _build_tool_registry(self, state: CodePilotState) -> ToolRegistry:
        return ToolRegistry(
            [
                ToolSpec(
                    "search_code",
                    "Search UTF-8 source files for a case-insensitive query.",
                    {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                    lambda arguments: self._search_code(arguments, state),
                ),
                ToolSpec(
                    "read_file",
                    "Read one repository-relative UTF-8 file.",
                    {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                    lambda arguments: self._read_file(arguments, state),
                ),
                ToolSpec(
                    "get_git_diff",
                    "Read the current Git diff without changing files.",
                    {
                        "type": "object",
                        "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
                        "required": [],
                        "additionalProperties": False,
                    },
                    lambda arguments: self._get_git_diff(arguments, state),
                ),
                ToolSpec(
                    "propose_patch",
                    "Propose a standard unified diff. This pauses for human approval before writing.",
                    {
                        "type": "object",
                        "properties": {
                            "patch": {"type": "string"},
                            "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        },
                        "required": ["patch", "paths"],
                        "additionalProperties": False,
                    },
                    lambda arguments: self._propose_patch(arguments, state),
                ),
                ToolSpec(
                    "run_tests",
                    "Run an allow-listed pytest command as an argument array.",
                    {
                        "type": "object",
                        "properties": {
                            "command": {"type": "array", "items": {"type": "string"}, "minItems": 1}
                        },
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                    lambda arguments: self._run_tests(arguments, state),
                ),
            ]
        )

    def _execute_tool(
        self,
        tool_call: ToolCall,
        registry: ToolRegistry,
        state: CodePilotState,
    ) -> tuple[dict[str, Any], bool]:
        name = tool_call.name
        self.trace.record(
            "tool.started",
            tool_name=name,
            arguments=self._safe_arguments(tool_call.arguments),
        )
        result = registry.execute(name, tool_call.arguments)
        if not result.ok:
            state.errors.append(f"{name}: {result.error}")
            self.trace.record("tool.completed", tool_name=name, ok=False, error=result.error)
            return result.as_message(), False
        self.trace.record("tool.completed", tool_name=name, ok=True, paused=result.pause)
        return result.as_message(), result.pause

    def _search_code(self, arguments: dict[str, Any], state: CodePilotState) -> ToolResult:
        state.phase = CodePilotPhase.SEARCHING
        hits = search_code(
            state.repo_path,
            self._required_string(arguments, "query"),
            max_results=self._optional_int(arguments, "max_results", 20),
        )
        return ToolResult.success({"hits": [hit.__dict__ for hit in hits]})

    def _read_file(self, arguments: dict[str, Any], state: CodePilotState) -> ToolResult:
        state.phase = CodePilotPhase.SEARCHING
        path = self._required_string(arguments, "path")
        content = read_file(state.repo_path, path)
        state.relevant_files = list(dict.fromkeys([*state.relevant_files, path]))
        return ToolResult.success(
            {"path": path, "content": content[:30_000], "truncated": len(content) > 30_000}
        )

    def _get_git_diff(self, arguments: dict[str, Any], state: CodePilotState) -> ToolResult:
        return ToolResult.success({"diff": get_git_diff(state.repo_path, self._optional_paths(arguments))})

    def _propose_patch(self, arguments: dict[str, Any], state: CodePilotState) -> ToolResult:
        if state.patch_applied:
            return ToolResult.failure(
                "a patch is already applied; run_tests must be called before another patch"
            )
        state.patch = self._required_string(arguments, "patch")
        paths = self._required_paths(arguments)
        state.patch_paths = paths
        state.relevant_files = list(dict.fromkeys([*state.relevant_files, *paths]))
        state.phase = CodePilotPhase.WAITING_APPROVAL
        self.trace.record("approval.requested", paths=state.relevant_files, patch_size=len(state.patch))
        if self.approval_callback is None:
            return ToolResult.success(
                {
                    "status": "waiting_approval",
                    "paths": state.relevant_files,
                    "message": "Human approval is required before applying this patch.",
                },
                pause=True,
            )
        if not self.approval_callback(state):
            state.fail("patch rejected by approval callback")
            return ToolResult.failure(state.errors[-1])
        return self._apply_patch_to_state(state)

    def _apply_patch_to_state(self, state: CodePilotState) -> ToolResult:
        """Apply the state patch through the single approval-controlled path."""

        if state.patch_applied:
            return ToolResult.failure("a patch is already applied")
        if not state.patch:
            return ToolResult.failure("no patch is pending approval")
        try:
            applied = apply_patch(
                state.repo_path,
                state.patch,
                approved=True,
                allowed_paths=state.patch_paths,
            )
        except (OSError, ValueError) as exc:
            return ToolResult.failure(str(exc))
        state.patch_applied = True
        state.phase = CodePilotPhase.TESTING
        return ToolResult.success(
            {
                "status": "applied",
                "touched_paths": applied.touched_paths,
                "message": "Patch applied. Run tests before declaring success.",
            }
        )

    def _run_tests(self, arguments: dict[str, Any], state: CodePilotState) -> ToolResult:
        command = self._required_command(arguments)
        state.phase = CodePilotPhase.TESTING
        test_result = run_tests(
            state.repo_path,
            command,
            cancel_check=lambda: state.cancel_requested,
        )
        state.tests_passed = test_result.passed
        state.test_output = "\n".join(part for part in (test_result.stdout, test_result.stderr) if part)
        if test_result.cancelled:
            state.cancel(test_result.error or "test command cancelled")
        elif not test_result.passed:
            state.phase = CodePilotPhase.FIXING if state.can_retry() else CodePilotPhase.FAILED
            state.attempts += 1
        return ToolResult.success(
            {
                "passed": test_result.passed,
                "returncode": test_result.returncode,
                "stdout": test_result.stdout[-20_000:],
                "stderr": test_result.stderr[-20_000:],
                "timed_out": test_result.timed_out,
                "cancelled": test_result.cancelled,
                "error": test_result.error,
            }
        )

    @staticmethod
    def _assistant_message(response: ModelResponse) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": response.content or None}
        if response.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in response.tool_calls
            ]
        return message

    @staticmethod
    def _required_string(arguments: dict[str, Any], key: str) -> str:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
        return value

    @staticmethod
    def _optional_int(arguments: dict[str, Any], key: str, default: int) -> int:
        value = arguments.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{key} must be an integer")
        return value

    @staticmethod
    def _required_paths(arguments: dict[str, Any]) -> list[str]:
        paths = arguments.get("paths")
        if not isinstance(paths, list) or not paths or not all(isinstance(path, str) for path in paths):
            raise ValueError("paths must be a non-empty string array")
        return list(dict.fromkeys(paths))

    @staticmethod
    def _optional_paths(arguments: dict[str, Any]) -> list[str]:
        paths = arguments.get("paths", [])
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            raise TypeError("paths must be a string array")
        return list(dict.fromkeys(paths))

    @staticmethod
    def _required_command(arguments: dict[str, Any]) -> list[str]:
        command = arguments.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
            raise ValueError("command must be a non-empty string array")
        return command

    @staticmethod
    def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        safe = dict(arguments)
        if "patch" in safe and isinstance(safe["patch"], str):
            safe["patch"] = f"<unified diff: {len(safe['patch'])} chars>"
        return safe
