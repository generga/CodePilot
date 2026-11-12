"""Small intentionally flawed paging service."""


def page_slice(items: list[str], page: int, size: int) -> list[str]:
    """Return one page of items."""

    start = (page - 1) * size
    return items[start : start + size]
