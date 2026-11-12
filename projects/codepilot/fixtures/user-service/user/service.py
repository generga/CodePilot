"""Small intentionally flawed service used by the CodePilot fixture."""


def normalize_email(email: str | None) -> str:
    """Return a normalized email address.

    The missing-input behavior is intentionally incomplete for the first
    CodePilot task.
    """

    return email.strip().lower()
