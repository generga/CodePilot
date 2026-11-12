import pytest

from user.service import normalize_email


def test_normalize_email_strips_and_lowercases():
    assert normalize_email("  USER@example.com ") == "user@example.com"


def test_normalize_email_rejects_missing_value():
    with pytest.raises(ValueError, match="email is required"):
        normalize_email(None)
