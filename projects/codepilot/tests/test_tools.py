import difflib
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
from codepilot.tools import (
    apply_patch,
    read_file,
    resolve_repo_path,
    run_tests,
    search_code,
)


def make_patch(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def test_search_code_returns_relative_locations(tmp_path: Path):
    source = tmp_path / "user.py"
    source.write_text("def register(email):\n    return email\n", encoding="utf-8")

    hits = search_code(tmp_path, "register")

    assert hits[0].path == "user.py"
    assert hits[0].line_number == 1


def test_read_file_rejects_path_traversal(tmp_path: Path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(ValueError):
        read_file(tmp_path, "../outside.txt")


def test_resolve_repo_path_accepts_nested_file(tmp_path: Path):
    path = resolve_repo_path(tmp_path, "src/app.py")
    assert path == (tmp_path / "src" / "app.py").resolve()


def test_apply_patch_requires_approval_and_updates_file(tmp_path: Path):
    source = tmp_path / "user.py"
    before = "def normalize(email):\n    return email.strip()\n"
    after = "def normalize(email):\n    if email is None:\n        raise ValueError(\"email is required\")\n    return email.strip()\n"
    source.write_text(before, encoding="utf-8")
    patch = make_patch("user.py", before, after)

    with pytest.raises(PermissionError):
        apply_patch(tmp_path, patch)

    result = apply_patch(tmp_path, patch, approved=True, allowed_paths=["user.py"])

    assert result.touched_paths == ("user.py",)
    assert result.modified_paths == ("user.py",)
    assert source.read_text(encoding="utf-8") == after


def test_apply_patch_rejects_traversal_before_writing(tmp_path: Path):
    patch = "--- a/../outside.py\n+++ b/../outside.py\n@@ -0,0 +1 @@\n+secret\n"

    with pytest.raises(ValueError, match="escapes repository"):
        apply_patch(tmp_path, patch, approved=True)
    assert not (tmp_path.parent / "outside.py").exists()


def test_apply_patch_rejects_context_mismatch(tmp_path: Path):
    source = tmp_path / "user.py"
    source.write_text("return original\n", encoding="utf-8")
    patch = make_patch("user.py", "return expected\n", "return changed\n")

    with pytest.raises(ValueError, match="does not match"):
        apply_patch(tmp_path, patch, approved=True)
    assert source.read_text(encoding="utf-8") == "return original\n"


def test_apply_patch_recovers_a_unique_line_number_offset(tmp_path: Path):
    source = tmp_path / "user.py"
    before = "line one\n\nline three\nline four\n"
    source.write_text(before, encoding="utf-8")
    patch = (
        "--- a/user.py\n"
        "+++ b/user.py\n"
        "@@ -4,2 +4,2 @@\n"
        " line three\n"
        "-line four\n"
        "+line changed\n"
    )

    apply_patch(tmp_path, patch, approved=True, allowed_paths=["user.py"])

    assert source.read_text(encoding="utf-8") == "line one\n\nline three\nline changed\n"


def test_apply_patch_accepts_model_hunk_with_inaccurate_header_counts(tmp_path: Path):
    source = tmp_path / "user.py"
    source.write_text(
        '"""Example."""\n\n\ndef normalize(value):\n    """Normalize a value."""\n\n    return value.strip()\n',
        encoding="utf-8",
    )
    patch = (
        "diff --git a/user.py b/user.py\n"
        "Index: user.py\n"
        "--- a/user.py\n"
        "+++ b/user.py\n"
        "@@ -1,10 +1,13 @@\n"
        ' """Example."""\n'
        " \n"
        " \n"
        " def normalize(value):\n"
        '     """Normalize a value."""\n'
        " \n"
        "+    if value is None:\n"
        '+        raise ValueError("value is required")\n'
        "+\n"
        "     return value.strip()\n"
    )

    apply_patch(tmp_path, patch, approved=True, allowed_paths=["user.py"])

    assert "if value is None" in source.read_text(encoding="utf-8")


def test_run_tests_executes_only_pytest(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sample.py").write_text(
        "def test_passes():\n    assert 2 + 2 == 4\n", encoding="utf-8"
    )

    result = run_tests(tmp_path, [sys.executable, "-m", "pytest", "-q"])

    assert result.passed is True
    assert result.returncode == 0
    assert result.timed_out is False


def test_run_tests_normalizes_plain_pytest_to_current_interpreter(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sample.py").write_text(
        "def test_passes():\n    assert True\n", encoding="utf-8"
    )

    result = run_tests(tmp_path, ["pytest", "tests/test_sample.py", "-q"])

    assert result.passed is True
    assert result.command[:3] == (sys.executable, "-m", "pytest")


def test_run_tests_rejects_shell_commands(tmp_path: Path):
    with pytest.raises(TypeError, match="shell string"):
        run_tests(tmp_path, "pytest -q")  # type: ignore[arg-type]

    with pytest.raises(PermissionError):
        run_tests(tmp_path, [sys.executable, "-c", "print('unsafe')"])


def test_run_tests_terminates_process_when_cancelled(tmp_path: Path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_slow.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(5)\n",
        encoding="utf-8",
    )
    cancelled = threading.Event()
    timer = threading.Timer(0.15, cancelled.set)
    timer.start()
    started = time.monotonic()
    try:
        result = run_tests(
            tmp_path,
            [sys.executable, "-m", "pytest", "tests", "-q"],
            timeout_seconds=10,
            cancel_check=cancelled.is_set,
        )
    finally:
        timer.cancel()

    assert result.cancelled is True
    assert result.passed is False
    assert result.error == "test command cancelled"
    assert time.monotonic() - started < 3


def test_fixture_patch_and_test_run_end_to_end(tmp_path: Path):
    fixture = Path(__file__).parents[1] / "fixtures" / "user-service"
    repo = tmp_path / "user-service"
    shutil.copytree(fixture, repo)
    source = repo / "user" / "service.py"
    before = source.read_text(encoding="utf-8")
    after = before.replace(
        "    return email.strip().lower()\n",
        '    if email is None:\n        raise ValueError("email is required")\n    return email.strip().lower()\n',
    )

    result = apply_patch(
        repo,
        make_patch("user/service.py", before, after),
        approved=True,
        allowed_paths=["user/service.py"],
    )
    test_result = run_tests(repo, [sys.executable, "-m", "pytest", "tests/test_user.py", "-q"])

    assert result.touched_paths == ("user/service.py",)
    assert test_result.passed is True
