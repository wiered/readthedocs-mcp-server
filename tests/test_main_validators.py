"""Tests for MCP helpers (validators, schemas, entity mapping; requires mcp dependency)."""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from readdocsserver.schemas.mcp import (
    ListedPage,
    LookupSymbolResponse,
    RelatedSymbolsResponse,
    SymbolGraphStatsResponse,
    SymbolLookupResult,
)
from readdocsserver.services.entity_mapping import entity_detail_from_dict
from readdocsserver.utils.text import slice_body_lines
from readdocsserver.utils.validators import (
    optional_edge_types,
    optional_entity_kind,
    optional_source_base,
    validate_line_slice,
    validate_limit,
    validate_list_pages_limit,
    validate_max_pages,
    validate_offset,
    validate_page_url,
    validate_seed_url,
    validate_symbol_name,
)


def test_validate_seed_url() -> None:
    assert validate_seed_url("  https://x.com/a  ") == "https://x.com/a"
    with pytest.raises(ValueError, match="empty"):
        validate_seed_url("  ")
    with pytest.raises(ValueError, match="http"):
        validate_seed_url("/relative")


def test_validate_max_pages() -> None:
    assert validate_max_pages(1) == 1
    assert validate_max_pages(5000) == 5000
    with pytest.raises(ValueError):
        validate_max_pages(0)
    with pytest.raises(ValueError):
        validate_max_pages(5001)


def test_validate_limit() -> None:
    assert validate_limit(15) == 15
    with pytest.raises(ValueError):
        validate_limit(0)
    with pytest.raises(ValueError):
        validate_limit(101)


def test_optional_entity_kind() -> None:
    assert optional_entity_kind(None) is None
    assert optional_entity_kind("  ") is None
    assert optional_entity_kind("Class") == "class"
    assert optional_entity_kind("method") == "method"
    with pytest.raises(ValueError, match="kind"):
        optional_entity_kind("function")


def test_validate_symbol_name() -> None:
    assert validate_symbol_name(" discord.ui.LayoutView ") == "discord.ui.LayoutView"
    with pytest.raises(ValueError, match="symbol_name"):
        validate_symbol_name("  ")


def test_optional_edge_types() -> None:
    assert optional_edge_types(None) is None
    assert optional_edge_types(["returns", " returns ", ""]) == ["returns"]
    assert optional_edge_types(["see_also", "requires"]) == [
        "see_also",
        "requires",
    ]
    with pytest.raises(ValueError, match="Unsupported edge_type"):
        optional_edge_types(["related_to"])


def test_validate_list_pages_limit() -> None:
    assert validate_list_pages_limit(1) == 1
    assert validate_list_pages_limit(500) == 500
    with pytest.raises(ValueError):
        validate_list_pages_limit(0)
    with pytest.raises(ValueError):
        validate_list_pages_limit(501)


def test_validate_offset() -> None:
    assert validate_offset(0) == 0
    with pytest.raises(ValueError):
        validate_offset(-1)
    with pytest.raises(ValueError):
        validate_offset(2_000_000)


def test_optional_source_base() -> None:
    assert optional_source_base(None) is None
    assert optional_source_base("  ") is None
    assert optional_source_base(" https://x/ ") == "https://x/"
    assert optional_source_base("https://x.com/en/latest") == "https://x.com/en/latest/"
    assert (
        optional_source_base("https://x.com/en/latest/page.html")
        == "https://x.com/en/latest/"
    )


def test_validate_page_url() -> None:
    assert validate_page_url("  https://x/p  ") == "https://x/p"
    with pytest.raises(ValueError, match="empty"):
        validate_page_url("  ")
    with pytest.raises(ValueError, match="http"):
        validate_page_url("/p")


def test_validate_line_slice() -> None:
    assert validate_line_slice(None, None) is None
    with pytest.raises(ValueError, match="both"):
        validate_line_slice(1, None)
    with pytest.raises(ValueError, match="start"):
        validate_line_slice(0, 5)
    with pytest.raises(ValueError, match="start"):
        validate_line_slice(3, 2)
    assert validate_line_slice(1, 5) == (1, 5)


def test_slice_body_lines() -> None:
    body = "a\nb\nc"
    text, n, s, e = slice_body_lines(body, 2, 2)
    assert text == "b"
    assert n == 3
    assert (s, e) == (2, 2)

    text2, n2, s2, e2 = slice_body_lines(body, 10, 20)
    assert text2 == ""
    assert n2 == 3
    assert s2 == 10

    empty, nz, _, _ = slice_body_lines("", 1, 1)
    assert empty == ""
    assert nz == 0


def test_entity_detail_model_conversion() -> None:
    detail = entity_detail_from_dict(
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


def test_listed_page_model_basic_fields() -> None:
    page = ListedPage.model_validate(
        {
            "url": "https://docs.example/api.html",
            "title": "API",
            "source_base": "https://docs.example/",
        }
    )

    assert page.url == "https://docs.example/api.html"
    assert page.title == "API"
    assert page.source_base == "https://docs.example/"


def test_symbol_lookup_model_conversion() -> None:
    response = LookupSymbolResponse(
        result=SymbolLookupResult.model_validate(
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


def test_related_symbols_model_conversion() -> None:
    response = RelatedSymbolsResponse.model_validate(
        {
            "found": True,
            "symbol": {
                "entity_id": "https://docs.example/api.html#pkg.Message",
                "qualname": "pkg.Message",
                "kind": "class",
                "page_url": "https://docs.example/api.html",
                "anchor": "pkg.Message",
            },
            "edges": [
                {
                    "direction": "in",
                    "edge_type": "returns",
                    "source_kind": "signature",
                    "param_name": "",
                    "confidence": 1.0,
                    "snippet": "copy() -> pkg.Message",
                    "source_page_url": "https://docs.example/api.html",
                    "line_start": 12,
                    "line_end": 12,
                    "relation_label": "returned_by",
                    "target": {
                        "entity_id": "https://docs.example/api.html#pkg.Service.send",
                        "qualname": "pkg.Service.send",
                        "kind": "method",
                        "page_url": "https://docs.example/api.html",
                        "anchor": "pkg.Service.send",
                    },
                }
            ],
        }
    )

    assert response.found is True
    assert response.symbol is not None
    assert response.symbol.qualname == "pkg.Message"
    assert response.edges[0].direction == "in"
    assert response.edges[0].relation_label == "returned_by"
    assert response.edges[0].snippet == "copy() -> pkg.Message"
    assert response.edges[0].target.qualname == "pkg.Service.send"


def test_symbol_graph_stats_model_conversion() -> None:
    stats = SymbolGraphStatsResponse.model_validate(
        {
            "source_base": "https://docs.example/",
            "edge_type_counts": {"references": 2},
            "source_kind_counts": {"xref": 2},
            "unresolved_xref_target_count": 1,
            "top_unresolved_targets": [{"target": "Missing", "count": 1}],
            "stale_edge_count": 0,
            "total_entities": 3,
            "total_pages": 1,
        }
    )

    assert stats.edge_type_counts["references"] == 2
    assert stats.top_unresolved_targets[0]["target"] == "Missing"
