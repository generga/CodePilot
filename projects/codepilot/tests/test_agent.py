import difflib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from agent_shared import (
    ModelResponse,
    ScriptedModel,
    Settings,
    ToolCall,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    TraceRecorder,
)
from codepilot import SqliteApprovalSessionStore
from codepilot.agent import CodePilotAgent
from codepilot.cli import build_model, main
from codepilot.state import CodePilotPhase


def make_patch(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def test_agent_runs_search_patch_approval_and_tests(tmp_path: Path):
    fixture = Path(__file__).parents[1] / "fixtures" / "user-service"
    repo = tmp_path / "user-service"
    shutil.copytree(fixture, repo)
    source = repo / "user" / "service.py"
    before = source.read_text(encoding="utf-8")
    after = before.replace(
        "    return email.strip().lower()\n",
        '    if email is None:\n        raise ValueError("email is required")\n    return email.strip().lower()\n',
    )
    patch = make_patch("user/service.py", before, after)
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=(ToolCall("1", "search_code", {"query": "normalize_email"}),)),
            ModelResponse(tool_calls=(ToolCall("2", "read_file", {"path": "user/service.py"}),)),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "3",
                        "propose_patch",
                        {"patch": patch, "paths": ["user/service.py"]},
                    ),
                )
            ),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "4",
                        "run_tests",
                        {"command": [sys.executable, "-m", "pytest", "tests/test_user.py", "-q"]},
                    ),
                )
            ),
            ModelResponse(content="修复完成，测试已通过。"),
        ]
    )

    trace = TraceRecorder()
    result = CodePilotAgent(model, approval_callback=lambda state: True, trace=trace).run(
        "修复邮箱为空时的异常", repo
    )

    assert result.state.phase == CodePilotPhase.COMPLETED
    assert result.state.patch_applied is True
    assert result.state.tests_passed is True
    assert result.final_message == "修复完成，测试已通过。"
    assert "approval.requested" in [event["name"] for event in trace.events()]
    assert "email is required" in source.read_text(encoding="utf-8")
    assert len(model.requests) == 5


def test_agent_pauses_without_approval_callback(tmp_path: Path):
    source = tmp_path / "service.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    patch = make_patch("service.py", source.read_text(encoding="utf-8"), "def run():\n    return 2\n")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall("1", "propose_patch", {"patch": patch, "paths": ["service.py"]}),
                )
            )
        ]
    )

    result = CodePilotAgent(model).run("change return value", tmp_path)

    assert result.state.phase == CodePilotPhase.WAITING_APPROVAL
    assert source.read_text(encoding="utf-8") == "def run():\n    return 1\n"


def test_agent_session_approval_resumes_with_original_model_context(tmp_path: Path):
    source = tmp_path / "service.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_service.py").write_text(
        "from service import run\n\n\ndef test_run():\n    assert run() == 2\n",
        encoding="utf-8",
    )
    patch = make_patch("service.py", source.read_text(encoding="utf-8"), "def run():\n    return 2\n")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall("patch-1", "propose_patch", {"patch": patch, "paths": ["service.py"]}),
                )
            ),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "tests-1",
                        "run_tests",
                        {"command": [sys.executable, "-m", "pytest", "tests", "-q"]},
                    ),
                )
            ),
            ModelResponse(content="修复完成，测试已通过。"),
        ]
    )

    paused = CodePilotAgent(model).run("change return value", tmp_path)

    assert paused.approval_required is True
    assert paused.session is not None
    resumed = paused.session.approve(True)

    assert resumed.state.phase == CodePilotPhase.COMPLETED
    assert resumed.final_message == "修复完成，测试已通过。"
    assert source.read_text(encoding="utf-8") == "def run():\n    return 2\n"
    resumed_request = model.requests[1]
    assert [message["role"] for message in resumed_request.messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    tool_message = resumed_request.messages[-1]
    assert tool_message["tool_call_id"] == "patch-1"
    assert json.loads(tool_message["content"])["status"] == "applied"


def test_agent_session_rejection_is_idempotently_closed(tmp_path: Path):
    source = tmp_path / "service.py"
    original = "def run():\n    return 1\n"
    source.write_text(original, encoding="utf-8")
    patch = make_patch("service.py", original, "def run():\n    return 2\n")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall("patch-1", "propose_patch", {"patch": patch, "paths": ["service.py"]}),
                )
            )
        ]
    )

    paused = CodePilotAgent(model).run("change return value", tmp_path)
    assert paused.session is not None

    rejected = paused.session.approve(False)

    assert rejected.state.phase == CodePilotPhase.FAILED
    assert rejected.state.patch_applied is False
    assert source.read_text(encoding="utf-8") == original
    with pytest.raises(RuntimeError, match="already been resolved"):
        paused.session.approve(True)


def test_agent_cannot_apply_a_file_outside_declared_patch_paths(tmp_path: Path):
    source = tmp_path / "service.py"
    original = "def run():\n    return 1\n"
    source.write_text(original, encoding="utf-8")
    patch = make_patch("service.py", original, "def run():\n    return 2\n")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "1",
                        "propose_patch",
                        {"patch": patch, "paths": ["other.py"]},
                    ),
                )
            ),
        ]
    )

    paused = CodePilotAgent(model).run("change return value", tmp_path)
    assert paused.session is not None
    result = paused.session.approve(True)

    assert result.state.phase == CodePilotPhase.FAILED
    assert result.state.patch_applied is False
    assert source.read_text(encoding="utf-8") == original


def test_agent_stops_at_tool_boundary_when_cancelled(tmp_path: Path):
    model = ScriptedModel(
        [ModelResponse(tool_calls=(ToolCall("1", "cancel", {}),))]
    )

    def build_registry(state):
        def cancel(_arguments):
            state.request_cancel()
            return ToolResult.success({"cancel_requested": True})

        return ToolRegistry(
            [
                ToolSpec(
                    "cancel",
                    "Request cancellation for the current run.",
                    {"type": "object", "properties": {}, "additionalProperties": False},
                    cancel,
                )
            ]
        )

    result = CodePilotAgent(model, tool_registry_factory=build_registry).run(
        "cancel this run", tmp_path
    )

    assert result.state.phase == CodePilotPhase.CANCELLED
    assert result.state.errors == ["run cancelled"]


def test_sqlite_session_can_resume_after_agent_restart(tmp_path: Path):
    source = tmp_path / "service.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_service.py").write_text(
        "from service import run\n\n\ndef test_run():\n    assert run() == 2\n",
        encoding="utf-8",
    )
    patch = make_patch("service.py", source.read_text(encoding="utf-8"), "def run():\n    return 2\n")
    store_path = tmp_path / "state" / "approvals.sqlite3"
    first_model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall("patch-1", "propose_patch", {"patch": patch, "paths": ["service.py"]}),
                )
            )
        ]
    )
    first_agent = CodePilotAgent(
        first_model,
        session_store=SqliteApprovalSessionStore(store_path),
    )

    paused = first_agent.run("change return value", tmp_path)

    assert paused.session_id is not None
    persisted = SqliteApprovalSessionStore(store_path).get(paused.session_id)
    assert persisted is not None
    assert persisted.status == "pending"
    assert persisted.state["issue"] == "change return value"

    second_model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "tests-1",
                        "run_tests",
                        {"command": [sys.executable, "-m", "pytest", "tests", "-q"]},
                    ),
                )
            ),
            ModelResponse(content="恢复后修复完成。"),
        ]
    )
    restarted_agent = CodePilotAgent(
        second_model,
        session_store=SqliteApprovalSessionStore(store_path),
    )
    restored = restarted_agent.resume(paused.session_id)
    assert restored.session is not None
    completed = restored.session.approve(True)

    assert completed.state.phase == CodePilotPhase.COMPLETED
    assert completed.final_message == "恢复后修复完成。"
    assert source.read_text(encoding="utf-8") == "def run():\n    return 2\n"
    assert SqliteApprovalSessionStore(store_path).get(paused.session_id).status == "approved"


def test_sqlite_session_allows_only_one_approval_decision(tmp_path: Path):
    source = tmp_path / "service.py"
    original = "def run():\n    return 1\n"
    source.write_text(original, encoding="utf-8")
    patch = make_patch("service.py", original, "def run():\n    return 2\n")
    store_path = tmp_path / "approvals.sqlite3"
    store = SqliteApprovalSessionStore(store_path)
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall("patch-1", "propose_patch", {"patch": patch, "paths": ["service.py"]}),
                )
            )
        ]
    )
    paused = CodePilotAgent(model, session_store=store).run("change return value", tmp_path)
    assert paused.session_id is not None

    first = CodePilotAgent(
        ScriptedModel([]),
        session_store=SqliteApprovalSessionStore(store_path),
    ).resume(paused.session_id)
    second = CodePilotAgent(
        ScriptedModel([]),
        session_store=SqliteApprovalSessionStore(store_path),
    ).resume(paused.session_id)

    assert first.session is not None
    first.session.approve(False)
    assert second.session is not None
    with pytest.raises(RuntimeError, match="already been resolved"):
        second.session.approve(False)
    assert source.read_text(encoding="utf-8") == original


def test_agent_accepts_custom_prompt_and_tool_registry_factory(tmp_path: Path):
    model = ScriptedModel([ModelResponse(content="no-op")])
    seen_states = []

    def build_registry(state):
        seen_states.append(state)
        return ToolRegistry()

    result = CodePilotAgent(
        model,
        system_prompt="You are an AIOps change assistant.",
        tool_registry_factory=build_registry,
    ).run("inspect a service incident", tmp_path)

    assert result.state.phase == CodePilotPhase.FAILED
    assert seen_states[0].issue == "inspect a service incident"
    assert model.requests[0].messages[0]["content"] == "You are an AIOps change assistant."


def test_offline_cli_completes_demo(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    trace_file = tmp_path / "traces" / "offline.jsonl"

    assert main(["--offline-demo", "--approve", "--trace-file", str(trace_file)]) == 0

    output = capsys.readouterr().out
    assert "状态: completed" in output
    assert "测试通过: True" in output
    trace_lines = trace_file.read_text(encoding="utf-8").splitlines()
    assert '"name": "run.started"' in trace_lines[0]
    assert '"name": "run.completed"' in trace_lines[-1]


def test_offline_cli_prints_patch_when_approval_is_missing(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(["--offline-demo"]) == 1

    output = capsys.readouterr().out
    assert "状态: waiting_approval" in output
    assert "待审批补丁:" in output
    assert "email is required" in output


def test_deepseek_model_requires_api_key(tmp_path: Path):
    settings = Settings(
        model_provider="deepseek",
        model_base_url="https://api.deepseek.com",
        model_name="deepseek-chat",
    )

    import pytest

    with pytest.raises(ValueError, match="MODEL_API_KEY"):
        build_model(settings, tmp_path, offline=False)


def test_sqlite_store_migrates_legacy_approval_status_constraint(tmp_path: Path):
    store_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(store_path) as connection:
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
                    status IN ('pending', 'approved', 'rejected', 'failed')
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO approval_sessions (
                session_id, repo_path, state_json, messages_json,
                next_step, tool_message_index, status, created_at, updated_at
            ) VALUES ('legacy-1', ?, '{}', '[]', 1, 0, 'pending', 'now', 'now')
            """,
            (str(tmp_path),),
        )

    store = SqliteApprovalSessionStore(store_path)
    store.cancel("legacy-1", resolved_by="bob")

    record = store.get("legacy-1")
    assert record is not None
    assert record.status == "cancelled"
    assert record.resolved_by == "bob"
