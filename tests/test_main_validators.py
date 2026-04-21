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


def test_optional_entity_kind() -> None:
    assert main._optional_entity_kind(None) is None
    assert main._optional_entity_kind("  ") is None
    assert main._optional_entity_kind("Class") == "class"
    assert main._optional_entity_kind("method") == "method"
    with pytest.raises(ValueError, match="kind"):
        main._optional_entity_kind("function")


def test_validate_symbol_name() -> None:
    assert main._validate_symbol_name(" discord.ui.LayoutView ") == "discord.ui.LayoutView"
    with pytest.raises(ValueError, match="symbol_name"):
        main._validate_symbol_name("  ")


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
    assert main._optional_source_base("https://x.com/en/latest") == "https://x.com/en/latest/"
    assert (
        main._optional_source_base("https://x.com/en/latest/page.html")
        == "https://x.com/en/latest/"
    )


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


def test_entity_detail_model_conversion() -> None:
    detail = main._entity_detail_from_dict(
        {
            "entity_id": "https://docs.example/api.html#views.LayoutView",
            "source_base": "https://docs.example/",
            "page_url": "https://docs.example/api.html",
            "anchor": "views.LayoutView",
            "kind": "class",
            "name": "LayoutView",
            "qualname": "views.LayoutView",
            "signature": "LayoutView",
            "summary": "Layout container.",
            "body_text": "Layout container.",
            "parent_entity_id": None,
            "line_start": 1,
            "line_end": 2,
            "params": [],
            "notes": [
                {
                    "ord": 0,
                    "kind": "versionadded",
                    "version": "1.2",
                    "text": "Initial class.",
                }
            ],
            "methods": [
                {
                    "entity_id": "https://docs.example/api.html#views.LayoutView.edit_message",
                    "source_base": "https://docs.example/",
                    "page_url": "https://docs.example/api.html",
                    "anchor": "views.LayoutView.edit_message",
                    "kind": "method",
                    "name": "edit_message",
                    "qualname": "views.LayoutView.edit_message",
                    "signature": "edit_message(text: str)",
                    "summary": "Edit a message.",
                    "body_text": "Edit a message.",
                    "parent_entity_id": "https://docs.example/api.html#views.LayoutView",
                    "line_start": 4,
                    "line_end": 6,
                    "params": [
                        {
                            "ord": 0,
                            "name": "text",
                            "type": "str",
                            "default": "",
                            "description": "Message text.",
                        }
                    ],
                    "notes": [
                        {
                            "ord": 0,
                            "kind": "warning",
                            "version": "",
                            "text": "May fail.",
                        }
                    ],
                    "methods": [],
                }
            ],
        }
    )
    assert detail.name == "LayoutView"
    assert detail.notes[0].kind == "versionadded"
    assert detail.methods[0].params[0].name == "text"
    assert detail.methods[0].notes[0].kind == "warning"


def test_symbol_lookup_model_conversion() -> None:
    response = main.LookupSymbolResponse(
        result=main.SymbolLookupResult.model_validate(
            {
                "found": True,
                "symbol_name": "discord.ui.LayoutView",
                "page_url": "https://docs.example/api.html",
                "anchor": "discord.ui.LayoutView",
                "url_with_anchor": "https://docs.example/api.html#discord.ui.LayoutView",
                "kind": "class",
                "name": "LayoutView",
                "qualname": "discord.ui.LayoutView",
                "line_start": 10,
                "line_end": 12,
                "context_start": 2,
                "context_end": 28,
                "context": "class discord.ui.LayoutView",
                "summary": "Layout container.",
                "entity_id": "https://docs.example/api.html#discord.ui.LayoutView",
            }
        )
    )

    assert response.result.url_with_anchor.endswith("#discord.ui.LayoutView")
    assert response.result.line_start == 10
    assert response.result.context_start == 2
