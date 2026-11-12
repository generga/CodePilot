"""Deterministic offline evaluation for the CodePilot workflow."""

from __future__ import annotations

import argparse
import difflib
import json
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path

from agent_shared import ModelResponse, ScriptedModel, ToolCall, TraceRecorder
from agentops import EvaluationReport, EvaluationRunner, EvaluationTask, load_tasks
from agentops.schema import RunSummary

from .agent import CodePilotAgent
from .tools import read_file

PROJECT_ROOT = Path(__file__).parents[2]
DEFAULT_TASKS_PATH = PROJECT_ROOT / "evals" / "tasks.json"
DEFAULT_FIXTURE_ROOT = PROJECT_ROOT


_TASK_FIXES: dict[str, dict[str, str]] = {
    "codepilot-001": {
        "query": "normalize_email",
        "path": "user/service.py",
        "old": "    return email.strip().lower()\n",
        "new": (
            '    if email is None:\n'
            '        raise ValueError("email is required")\n'
            '    return email.strip().lower()\n'
        ),
    },
    "codepilot-004": {
        "query": "normalize_email",
        "path": "user/service.py",
        "old": "    return email.strip().lower()\n",
        "new": (
            '    if email is None:\n'
            '        raise ValueError("email is required")\n'
            '    return email.strip().lower()\n'
        ),
    },
    "codepilot-002": {
        "query": "validate_amount",
        "path": "order/service.py",
        "old": "    return float(amount)\n",
        "new": (
            "    value = float(amount)\n"
            "    if value < 0:\n"
            '        raise ValueError("amount must be non-negative")\n'
            "    return value\n"
        ),
    },
    "codepilot-005": {
        "query": "validate_amount",
        "path": "order/service.py",
        "old": "    return float(amount)\n",
        "new": (
            "    value = float(amount)\n"
            "    if value < 0:\n"
            '        raise ValueError("amount must be non-negative")\n'
            "    return value\n"
        ),
    },
    "codepilot-003": {
        "query": "page_slice",
        "path": "paging/service.py",
        "old": "    start = (page - 1) * size\n",
        "new": (
            '    if page <= 0 or size <= 0:\n'
            '        raise ValueError("page and size must be positive")\n'
            "    start = (page - 1) * size\n"
        ),
    },
}


def _unified_patch(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _task_fix(task: EvaluationTask) -> dict[str, str]:
    try:
        return _TASK_FIXES[task.task_id]
    except KeyError as exc:
        raise ValueError(f"no offline model fixture for task {task.task_id}") from exc


def _pytest_command(task: EvaluationTask) -> list[str]:
    raw_command = task.payload.get("test_command")
    if not isinstance(raw_command, str) or not raw_command.strip():
        raise ValueError(f"task {task.task_id} has no test_command")
    parts = shlex.split(raw_command)
    if not parts or Path(parts[0]).name.casefold() not in {"pytest", "pytest.exe"}:
        raise ValueError(f"task {task.task_id} must use pytest as its test command")
    return [sys.executable, "-m", "pytest", *parts[1:], "-q"]


def build_offline_model(task: EvaluationTask, repo: Path) -> ScriptedModel:
    """Build a five-turn model replay for one fixed task.

    This simulates the model side of the workflow while keeping evaluation
    independent from network access, API keys, and provider-specific output.
    """

    fix = _task_fix(task)
    before = read_file(repo, fix["path"])
    if before.count(fix["old"]) != 1:
        raise ValueError(f"offline fixture does not match expected source for {task.task_id}")
    after = before.replace(fix["old"], fix["new"], 1)
    patch = _unified_patch(fix["path"], before, after)
    return ScriptedModel(
        [
            ModelResponse(
                tool_calls=(ToolCall("1", "search_code", {"query": fix["query"]}),)
            ),
            ModelResponse(
                tool_calls=(ToolCall("2", "read_file", {"path": fix["path"]}),)
            ),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "3",
                        "propose_patch",
                        {"patch": patch, "paths": [fix["path"]]},
                    ),
                )
            ),
            ModelResponse(
                tool_calls=(ToolCall("4", "run_tests", {"command": _pytest_command(task)}),)
            ),
            ModelResponse(content="修复完成，测试已通过。"),
        ]
    )


def _fixture_repo(task: EvaluationTask, fixture_root: Path, target_root: Path) -> Path:
    raw_repo = task.payload.get("repo")
    if not isinstance(raw_repo, str) or not raw_repo.strip():
        raise ValueError(f"task {task.task_id} has no repo")
    source = (fixture_root / raw_repo).resolve()
    try:
        source.relative_to(fixture_root.resolve())
    except ValueError as exc:
        raise ValueError(f"task {task.task_id} repo escapes fixture root") from exc
    if not source.is_dir():
        raise FileNotFoundError(str(source))
    target = target_root / source.name
    shutil.copytree(source, target)
    return target


def _expected_paths(task: EvaluationTask) -> list[str]:
    paths = task.payload.get("expected_files", [])
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise ValueError(f"task {task.task_id} expected_files must be a string array")
    return paths


def _execute_task(task: EvaluationTask, fixture_root: Path) -> RunSummary:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix=f"codepilot-{task.task_id}-") as work_dir:
        repo = _fixture_repo(task, fixture_root, Path(work_dir))
        model = build_offline_model(task, repo)
        trace = TraceRecorder()
        result = CodePilotAgent(
            model,
            max_steps=10,
            approval_callback=lambda state: True,
            trace=trace,
        ).run(str(task.payload.get("issue", "")), repo)

        expected_paths = set(_expected_paths(task))
        touched_paths = set(result.state.relevant_files)
        tool_calls = sum(event["name"] == "tool.completed" for event in trace.events())
        completed = (
            result.state.phase.value == "completed"
            and result.state.patch_applied
            and result.state.tests_passed is True
            and expected_paths.issubset(touched_paths)
        )
        status = "completed" if completed else "failed"
        return RunSummary(
            run_id=f"codepilot-offline-{task.task_id}",
            project="codepilot",
            task_id=task.task_id,
            status=status,
            steps=len(model.requests),
            latency_ms=int((time.perf_counter() - started) * 1000),
            tool_errors=len(result.state.errors),
            tool_calls=tool_calls,
        )


def run_evaluation(
    tasks: Iterable[EvaluationTask] | None = None,
    *,
    tasks_path: str | Path = DEFAULT_TASKS_PATH,
    fixture_root: str | Path = DEFAULT_FIXTURE_ROOT,
) -> EvaluationReport:
    """Run the offline CodePilot task set through the shared evaluator."""

    selected_tasks = list(tasks) if tasks is not None else load_tasks(tasks_path)
    root = Path(fixture_root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(str(fixture_root))
    runner = EvaluationRunner(lambda task: _execute_task(task, root))
    return runner.run(selected_tasks)


def _format_report(report: EvaluationReport) -> str:
    lines = [
        f"任务数: {len(report.results)}",
        f"成功率: {report.success_rate:.1%}",
        f"平均延迟: {report.average_latency_ms:.0f} ms",
        f"p50/p95 延迟: {report.p50_latency_ms:.0f}/{report.p95_latency_ms:.0f} ms",
        f"平均步数: {report.average_steps:.1f}",
        f"平均成本: {report.average_cost:.4f}",
        f"工具错误率: {report.tool_error_rate:.1%}",
    ]
    for result, run in zip(report.results, report.runs):
        lines.append(
            f"- {result.task_id}: {run.status}, steps={run.steps}, "
            f"latency={run.latency_ms} ms, tool_errors={run.tool_errors}"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate CodePilot with deterministic offline tasks")
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS_PATH)
    parser.add_argument("--fixture-root", type=Path, default=DEFAULT_FIXTURE_ROOT)
    parser.add_argument("--json", action="store_true", help="print the machine-readable report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_evaluation(tasks_path=args.tasks, fixture_root=args.fixture_root)
    except (FileNotFoundError, NotADirectoryError, TypeError, ValueError) as exc:
        print(f"评估配置错误: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) if args.json else _format_report(report))
    return 0 if report.success_rate == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
