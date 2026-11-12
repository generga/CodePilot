"""Safe, deterministic repository tools for the first CodePilot MVP."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
_ALLOWED_TEST_EXECUTABLES = {"pytest", "pytest.exe", "python", "python.exe", "py", "py.exe"}
_FORBIDDEN_PYTEST_FLAGS = {"--pdb", "--trace", "--basetemp", "--rootdir"}

SKIP_DIRECTORIES = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules"}


@dataclass(frozen=True)
class SearchHit:
    path: str
    line_number: int
    line: str


@dataclass(frozen=True)
class PatchApplicationResult:
    touched_paths: tuple[str, ...]
    created_paths: tuple[str, ...]
    modified_paths: tuple[str, ...]


@dataclass(frozen=True)
class TestRunResult:
    command: tuple[str, ...]
    passed: bool
    returncode: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    cancelled: bool = False
    error: str | None = None


@dataclass(frozen=True)
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: tuple[_Hunk, ...]


def resolve_repo_path(repo_root: str | Path, relative_path: str | Path) -> Path:
    """Resolve a path and reject traversal outside the repository root."""

    root = Path(repo_root).resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("path is outside the repository root") from exc
    return candidate


def read_file(repo_root: str | Path, relative_path: str | Path) -> str:
    path = resolve_repo_path(repo_root, relative_path)
    if not path.is_file():
        raise FileNotFoundError(str(relative_path))
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def search_code(
    repo_root: str | Path,
    query: str,
    *,
    max_results: int = 20,
) -> list[SearchHit]:
    """Perform a case-insensitive baseline search over UTF-8 text files."""

    if not query.strip():
        raise ValueError("query must not be empty")
    if max_results <= 0:
        return []

    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(str(repo_root))

    needle = query.casefold()
    hits: list[SearchHit] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or SKIP_DIRECTORIES.intersection(path.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        relative = path.relative_to(root).as_posix()
        for line_number, line in enumerate(lines, start=1):
            if needle in line.casefold():
                hits.append(SearchHit(relative, line_number, line.strip()))
                if len(hits) >= max_results:
                    return hits
    return hits


def _normalize_patch_path(raw_path: str, prefix: str) -> str | None:
    path = raw_path.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    path = path.removeprefix(prefix)
    path = path.replace("\\", "/")
    if not path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise ValueError(f"invalid patch path: {raw_path}")
    parts = [part for part in path.split("/") if part not in {"", "."}]
    if ".." in parts or not parts:
        raise ValueError(f"patch path escapes repository: {raw_path}")
    return "/".join(parts)


def _parse_file_patches(patch: str) -> list[_FilePatch]:
    if not patch.strip():
        raise ValueError("patch must not be empty")

    lines = patch.splitlines(keepends=True)
    file_patches: list[_FilePatch] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("--- "):
            index += 1
            continue

        old_path = _normalize_patch_path(line[4:].rstrip("\r\n"), "a/")
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise ValueError("patch is missing its new-file header")
        new_path = _normalize_patch_path(lines[index][4:].rstrip("\r\n"), "b/")
        index += 1

        hunks: list[_Hunk] = []
        while index < len(lines):
            current = lines[index]
            if current.startswith(("--- ", "diff --git ")):
                break
            if not current.startswith("@@ "):
                index += 1
                continue

            match = _HUNK_HEADER.match(current.rstrip("\r\n"))
            if match is None:
                raise ValueError(f"invalid hunk header: {current.rstrip()}")
            old_start = int(match.group("old_start"))
            old_count = int(match.group("old_count") or "1")
            new_start = int(match.group("new_start"))
            new_count = int(match.group("new_count") or "1")
            index += 1

            hunk_lines: list[tuple[str, str]] = []
            while index < len(lines):
                hunk_line = lines[index]
                if hunk_line.startswith(("@@ ", "--- ", "diff --git ")):
                    break
                if hunk_line.startswith("\\ No newline at end of file"):
                    raise ValueError("patches without a final newline are not supported yet")
                if not hunk_line or hunk_line[0] not in {" ", "+", "-"}:
                    break
                marker = hunk_line[0]
                content = hunk_line[1:]
                hunk_lines.append((marker, content))
                index += 1

            old_seen = sum(marker != "+" for marker, _ in hunk_lines)
            new_seen = sum(marker != "-" for marker, _ in hunk_lines)
            if old_seen != old_count or new_seen != new_count:
                # Some OpenAI-compatible models miscount blank/context lines in
                # the header while still returning a complete, valid hunk.
                # Validate the actual body and use its authoritative counts.
                if not hunk_lines:
                    raise ValueError("hunk line counts do not match the hunk header")
                old_count = old_seen
                new_count = new_seen
            hunks.append(_Hunk(old_start, old_count, new_start, new_count, tuple(hunk_lines)))

        if not hunks:
            raise ValueError("file patch has no hunks")
        if old_path is not None and new_path is not None and old_path != new_path:
            raise ValueError("file renames are not supported by the MVP patch tool")
        if old_path is None and new_path is None:
            raise ValueError("file patch has no target path")
        file_patches.append(_FilePatch(old_path, new_path, tuple(hunks)))

    if not file_patches:
        raise ValueError("no unified file patches found")
    return file_patches


def _read_lines(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read().splitlines(keepends=True)


def _apply_file_patch(root: Path, file_patch: _FilePatch) -> tuple[str, str | None]:
    source_path = resolve_repo_path(root, file_patch.old_path) if file_patch.old_path else None
    target_name = file_patch.new_path or file_patch.old_path
    assert target_name is not None
    target_path = resolve_repo_path(root, target_name)

    if source_path is not None:
        if not source_path.is_file():
            raise FileNotFoundError(file_patch.old_path)
        original_lines = _read_lines(source_path)
    else:
        original_lines = []
        if target_path.exists():
            raise FileExistsError(target_name)

    newline = "\r\n" if any(line.endswith("\r\n") for line in original_lines) else "\n"

    def normalize_patch_line(line: str) -> str:
        return line.rstrip("\r\n") + newline

    def line_matches(original: str, expected: str) -> bool:
        return original.replace("\r\n", "\n") == expected.replace("\r\n", "\n")

    result: list[str] = []
    source_cursor = 0
    for hunk in file_patch.hunks:
        hunk_start = 0 if hunk.old_start == 0 else hunk.old_start - 1
        expected_lines = tuple(
            content for marker, content in hunk.lines if marker in {" ", "-"}
        )
        if expected_lines:
            candidates = [
                candidate
                for candidate in range(source_cursor, len(original_lines) - len(expected_lines) + 1)
                if all(
                    line_matches(original_lines[candidate + offset], expected)
                    for offset, expected in enumerate(expected_lines)
                )
            ]
            if hunk_start in candidates:
                actual_start = hunk_start
            elif len(candidates) == 1:
                actual_start = candidates[0]
            elif not candidates:
                raise ValueError(f"patch context does not match: {target_name}")
            else:
                raise ValueError(f"patch context is ambiguous: {target_name}")
        else:
            actual_start = hunk_start
        if actual_start < source_cursor or actual_start > len(original_lines):
            raise ValueError(f"hunk starts outside source file: {target_name}")
        result.extend(original_lines[source_cursor:actual_start])
        source_cursor = actual_start

        old_seen = 0
        new_seen = 0
        for marker, content in hunk.lines:
            if marker == " ":
                if source_cursor >= len(original_lines) or not line_matches(
                    original_lines[source_cursor], content
                ):
                    raise ValueError(f"patch context does not match: {target_name}")
                result.append(original_lines[source_cursor])
                source_cursor += 1
                old_seen += 1
                new_seen += 1
            elif marker == "-":
                if source_cursor >= len(original_lines) or not line_matches(
                    original_lines[source_cursor], content
                ):
                    raise ValueError(f"patch removal does not match: {target_name}")
                source_cursor += 1
                old_seen += 1
            else:
                result.append(normalize_patch_line(content))
                new_seen += 1

        if old_seen != hunk.old_count or new_seen != hunk.new_count:
            raise ValueError(f"hunk counts do not match while applying: {target_name}")

    result.extend(original_lines[source_cursor:])
    new_content = "".join(result)
    if source_path is None:
        change_kind = "created"
    else:
        change_kind = "modified"
    return new_content, change_kind


def apply_patch(
    repo_root: str | Path,
    patch: str,
    *,
    approved: bool = False,
    allowed_paths: Sequence[str] | None = None,
) -> PatchApplicationResult:
    """Validate and apply a limited unified diff after explicit approval."""

    if not approved:
        raise PermissionError("applying a patch requires approved=True")
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(str(repo_root))

    file_patches = _parse_file_patches(patch)
    normalized_allowed = {
        _normalize_patch_path(str(path), "") for path in allowed_paths
    } if allowed_paths is not None else None

    pending: list[tuple[Path, str, str, str]] = []
    seen_paths: set[str] = set()
    for file_patch in file_patches:
        touched_path = file_patch.new_path or file_patch.old_path
        assert touched_path is not None
        if touched_path in seen_paths:
            raise ValueError(f"duplicate file patch: {touched_path}")
        seen_paths.add(touched_path)
        if normalized_allowed is not None and touched_path not in normalized_allowed:
            raise PermissionError(f"patch touches a path outside the approved set: {touched_path}")
        if file_patch.new_path is None:
            raise PermissionError("file deletion is disabled in the MVP patch tool")
        new_content, change_kind = _apply_file_patch(root, file_patch)
        target_path = resolve_repo_path(root, file_patch.new_path)
        pending.append((target_path, touched_path, change_kind or "modified", new_content))

    for target_path, _, _, new_content in pending:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(new_content)

    created = tuple(path for _, path, kind, _ in pending if kind == "created")
    modified = tuple(path for _, path, kind, _ in pending if kind == "modified")
    return PatchApplicationResult(tuple(path for _, path, _, _ in pending), created, modified)


def _validate_test_command(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, bytes)):
        raise TypeError("test command must be a sequence of arguments, not a shell string")
    parts = tuple(str(part) for part in command)
    if not parts or any("\x00" in part for part in parts):
        raise ValueError("test command must contain non-empty arguments")

    executable = Path(parts[0]).name.casefold()
    if executable not in _ALLOWED_TEST_EXECUTABLES:
        raise PermissionError("only pytest or python -m pytest commands are allowed")
    if executable in {"pytest", "pytest.exe"}:
        pytest_args = parts[1:]
        normalized = (sys.executable, "-m", "pytest", *pytest_args)
    else:
        if len(parts) < 3 or parts[1] != "-m" or parts[2] != "pytest":
            raise PermissionError("Python test commands must use -m pytest")
        pytest_args = parts[3:]
        normalized = (sys.executable, "-m", "pytest", *pytest_args)
    if any(arg in _FORBIDDEN_PYTEST_FLAGS for arg in pytest_args):
        raise PermissionError("interactive or externally rooted pytest flags are not allowed")
    return normalized


def run_tests(
    repo_root: str | Path,
    command: Sequence[str],
    *,
    timeout_seconds: float = 30.0,
    cancel_check: Callable[[], bool] | None = None,
) -> TestRunResult:
    """Run an allow-listed pytest command without invoking a shell."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(str(repo_root))
    parts = _validate_test_command(command)
    started = time.monotonic()
    if cancel_check is not None and cancel_check():
        return TestRunResult(
            parts,
            False,
            None,
            "",
            "",
            0,
            cancelled=True,
            error="test command cancelled",
        )
    if cancel_check is not None:
        return _run_tests_cancellable(root, parts, timeout_seconds, cancel_check, started)
    try:
        completed = subprocess.run(
            parts,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(errors="replace")
        return TestRunResult(parts, False, None, stdout, stderr, duration_ms, timed_out=True, error="test command timed out")
    except OSError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        return TestRunResult(parts, False, None, "", "", duration_ms, error=str(exc))

    duration_ms = int((time.monotonic() - started) * 1000)
    return TestRunResult(
        parts,
        completed.returncode == 0,
        completed.returncode,
        completed.stdout,
        completed.stderr,
        duration_ms,
    )


def _run_tests_cancellable(
    root: Path,
    command: tuple[str, ...],
    timeout_seconds: float,
    cancel_check: Callable[[], bool],
    started: float,
) -> TestRunResult:
    process = subprocess.Popen(
        command,
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    timed_out = False
    cancelled = False
    try:
        while True:
            if cancel_check():
                cancelled = True
                process.kill()
                break
            if time.monotonic() - started >= timeout_seconds:
                timed_out = True
                process.kill()
                break
            try:
                stdout, stderr = process.communicate(timeout=0.05)
                return TestRunResult(
                    command,
                    process.returncode == 0,
                    process.returncode,
                    stdout,
                    stderr,
                    int((time.monotonic() - started) * 1000),
                )
            except subprocess.TimeoutExpired:
                continue
        stdout, stderr = process.communicate()
    except OSError as exc:
        process.kill()
        stdout, stderr = process.communicate()
        return TestRunResult(
            command,
            False,
            None,
            stdout,
            stderr,
            int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
            cancelled=cancelled,
            error=str(exc),
        )
    return TestRunResult(
        command,
        False,
        None,
        stdout,
        stderr,
        int((time.monotonic() - started) * 1000),
        timed_out=timed_out,
        cancelled=cancelled,
        error="test command cancelled" if cancelled else "test command timed out",
    )


def get_git_diff(repo_root: str | Path, paths: Sequence[str] | None = None) -> str:
    """Return the current Git diff for the repository or selected files."""

    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(str(repo_root))
    normalized_paths = []
    for path in paths or ():
        normalized = _normalize_patch_path(str(path), "")
        assert normalized is not None
        resolve_repo_path(root, normalized)
        normalized_paths.append(normalized)
    command = ["git", "-C", str(root), "diff", "--no-ext-diff", "--unified=3", "--"]
    command.extend(normalized_paths)
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git diff failed")
    return completed.stdout
