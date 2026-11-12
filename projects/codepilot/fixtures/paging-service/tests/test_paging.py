import pytest

from paging.service import page_slice


def test_page_slice_returns_requested_page():
    assert page_slice(["a", "b", "c", "d"], 2, 2) == ["c", "d"]


@pytest.mark.parametrize("page,size", [(0, 2), (1, 0), (1, -1)])
def test_page_slice_rejects_invalid_bounds(page, size):
    with pytest.raises(ValueError, match="page and size must be positive"):
        page_slice(["a"], page, size)
