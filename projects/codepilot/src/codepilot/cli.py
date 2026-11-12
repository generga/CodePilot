"""Command-line entry point for CodePilot."""

from __future__ import annotations

import argparse
import difflib
import shutil
import sys
from pathlib import Path

from agent_shared import (
    ModelResponse,
    OpenAICompatibleClient,
    ScriptedModel,
    Settings,
    ToolCall,
    TraceRecorder,
)

from .agent import CodePilotAgent
from .tools import read_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the CodePilot coding agent")
    parser.add_argument("--repo", type=Path, help="local repository to inspect")
    parser.add_argument("--issue", help="bug or change request")
    parser.add_argument(
        "--offline-demo",
        action="store_true",
        help="run the bundled fixture with a deterministic scripted model",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="approve the proposed patch (only for local development)",
    )
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument(
        "--trace-file",
        type=Path,
        help="write structured run events to a local JSONL file",
    )
    return parser


def _fixture_patch(repo: Path) -> str:
    before = read_file(repo, "user/service.py")
    after = before.replace(
        "    return email.strip().lower()\n",
        '    if email is None:\n        raise ValueError("email is required")\n    return email.strip().lower()\n',
    )
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="a/user/service.py",
            tofile="b/user/service.py",
        )
    )


def build_offline_model(repo: Path) -> ScriptedModel:
    patch = _fixture_patch(repo)
    return ScriptedModel(
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


def copy_demo_fixture() -> Path:
    source = Path(__file__).parents[2] / "fixtures" / "user-service"
    target = Path.cwd() / ".runtime" / "codepilot-user-service"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    return target


def build_model(settings: Settings, repo: Path, offline: bool):
    if offline:
        return build_offline_model(repo)
    if not settings.model_base_url or not settings.model_name:
        raise ValueError(
            "real model mode requires MODEL_BASE_URL and MODEL_NAME; use --offline-demo first"
        )
    if settings.model_provider == "deepseek" and not settings.model_api_key:
        raise ValueError("DeepSeek mode requires MODEL_API_KEY")
    return OpenAICompatibleClient(
        base_url=settings.model_base_url,
        model=settings.model_name,
        api_key=settings.model_api_key,
        timeout_seconds=settings.model_timeout_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.offline_demo:
        repo = copy_demo_fixture()
        issue = "修复用户注册时邮箱为空导致的异常"
    elif args.repo and args.issue:
        repo = args.repo.resolve()
        issue = args.issue
    else:
        print("请提供 --repo 和 --issue，或使用 --offline-demo。", file=sys.stderr)
        return 2

    settings = Settings.from_env()
    try:
        model = build_model(settings, repo, args.offline_demo)
    except ValueError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    trace = TraceRecorder()
    trace.record(
        "run.started",
        project="codepilot",
        provider="scripted" if args.offline_demo else settings.model_provider,
        model="scripted-offline" if args.offline_demo else settings.model_name,
        repo=str(repo),
        issue=issue,
    )
    agent = CodePilotAgent(
        model,
        max_steps=args.max_steps,
        approval_callback=(lambda state: args.approve) if args.approve else None,
        trace=trace,
    )
    result = agent.run(issue, repo)
    trace.record(
        "run.completed",
        phase=result.state.phase.value,
        patch_applied=result.state.patch_applied,
        tests_passed=result.state.tests_passed,
        error_count=len(result.state.errors),
    )
    if args.trace_file:
        try:
            trace.write_jsonl(args.trace_file)
        except OSError as exc:
            print(f"Trace 写入失败: {exc}", file=sys.stderr)
            return 2
    print(f"状态: {result.state.phase.value}")
    print(f"仓库: {repo}")
    print(f"相关文件: {', '.join(result.state.relevant_files) or '无'}")
    print(f"补丁已应用: {result.state.patch_applied}")
    print(f"测试通过: {result.state.tests_passed}")
    if result.state.patch and not result.state.patch_applied:
        print("待审批补丁:")
        print(result.state.patch)
    if result.final_message:
        print(f"结论: {result.final_message}")
    if result.state.errors:
        print("错误:")
        for error in result.state.errors:
            print(f"- {error}")
    print(f"Trace 事件: {len(trace.events())}")
    return 0 if result.state.phase.value == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
