from pathlib import Path

from codepilot.state import CodePilotPhase, CodePilotState


def test_state_stops_after_three_attempts():
    state = CodePilotState(issue="fix bug", repo_path=Path("."), attempts=3)
    assert state.can_retry() is False


def test_fail_records_error_and_phase():
    state = CodePilotState(issue="fix bug", repo_path=Path("."))
    state.fail("test timed out")
    assert state.phase == CodePilotPhase.FAILED
    assert state.errors == ["test timed out"]


def test_state_does_not_mark_patch_as_applied_by_default():
    state = CodePilotState(issue="fix bug", repo_path=Path("."))
    assert state.patch_applied is False
    assert state.patch_paths == []


def test_state_supports_cooperative_cancellation():
    state = CodePilotState(issue="fix bug", repo_path=Path("."))

    state.request_cancel()
    state.cancel()

    assert state.cancel_requested is True
    assert state.phase == CodePilotPhase.CANCELLED
    assert state.errors == ["run cancelled"]
