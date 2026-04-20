"""Tests for helpers in readdocsserver.main (requires mcp dependency)."""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from readdocsserver import main


def test_validate_seed_url() -> None:
    assert main._validate_seed_url("  https://x.com/a  ") == "https://x.com/a"
    with pytest.raises(ValueError, match="empty"):
        main._validate_seed_url("  ")
    with pytest.raises(ValueError, match="http"):
        main._validate_seed_url("/relative")


def test_validate_max_pages() -> None:
    assert main._validate_max_pages(1) == 1
    assert main._validate_max_pages(5000) == 5000
    with pytest.raises(ValueError):
        main._validate_max_pages(0)
    with pytest.raises(ValueError):
        main._validate_max_pages(5001)


def test_validate_limit() -> None:
    assert main._validate_limit(15) == 15
    with pytest.raises(ValueError):
        main._validate_limit(0)
    with pytest.raises(ValueError):
        main._validate_limit(101)


def test_validate_list_pages_limit() -> None:
    assert main._validate_list_pages_limit(1) == 1
    assert main._validate_list_pages_limit(500) == 500
    with pytest.raises(ValueError):
        main._validate_list_pages_limit(0)
    with pytest.raises(ValueError):
        main._validate_list_pages_limit(501)


def test_validate_offset() -> None:
    assert main._validate_offset(0) == 0
    with pytest.raises(ValueError):
        main._validate_offset(-1)
    with pytest.raises(ValueError):
        main._validate_offset(2_000_000)


def test_optional_source_base() -> None:
    assert main._optional_source_base(None) is None
    assert main._optional_source_base("  ") is None
    assert main._optional_source_base(" https://x/ ") == "https://x/"


def test_validate_page_url() -> None:
    assert main._validate_page_url("  https://x/p  ") == "https://x/p"
    with pytest.raises(ValueError, match="empty"):
        main._validate_page_url("  ")
    with pytest.raises(ValueError, match="http"):
        main._validate_page_url("/p")


def test_validate_line_slice() -> None:
    assert main._validate_line_slice(None, None) is None
    with pytest.raises(ValueError, match="both"):
        main._validate_line_slice(1, None)
    with pytest.raises(ValueError, match="start"):
        main._validate_line_slice(0, 5)
    with pytest.raises(ValueError, match="start"):
        main._validate_line_slice(3, 2)
    assert main._validate_line_slice(1, 5) == (1, 5)


def test_slice_body_lines() -> None:
    body = "a\nb\nc"
    text, n, s, e = main._slice_body_lines(body, 2, 2)
    assert text == "b"
    assert n == 3
    assert (s, e) == (2, 2)

    text2, n2, s2, e2 = main._slice_body_lines(body, 10, 20)
    assert text2 == ""
    assert n2 == 3
    assert s2 == 10

    empty, nz, _, _ = main._slice_body_lines("", 1, 1)
    assert empty == ""
    assert nz == 0
