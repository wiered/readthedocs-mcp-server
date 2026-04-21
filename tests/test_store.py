"""Tests for readdocsserver.store (FTS index, chunking, search)."""

from __future__ import annotations

from pathlib import Path

from readdocsserver.store import (
    DocIndex,
    _body_to_chunks,
    _body_to_chunks_with_lines,
    _collect_query_pieces,
    _context_line_from_chunk,
    _fts_match_stages,
    _fts_match_queries,
)


def test_fts_match_queries_empty() -> None:
    assert _fts_match_queries("") == (None, None)
    assert _fts_match_queries("   ") == (None, None)
    assert _fts_match_queries("a x") == (None, None)  # only 1-char tokens after strip


def test_fts_match_queries_strong_and_weak() -> None:
    primary, fallback = _fts_match_queries("send_photo InputFile photo")
    assert primary is not None
    assert '"send_photo"' in primary
    assert '"InputFile"' in primary
    assert '"photo"' in primary
    # One weak token is parenthesized; " OR " appears only between 2+ weak tokens.
    assert primary.endswith('("photo")') or " OR " in primary

    primary2, _ = _fts_match_queries("send_photo foo bar")
    assert primary2 is not None
    assert '"send_photo"' in primary2
    assert " OR " in primary2
    assert '"foo"' in primary2 and '"bar"' in primary2

    assert fallback is not None
    assert " OR " in fallback


def test_fts_match_stages_auto_free_long_query() -> None:
    stages = _fts_match_stages("send_photo photo URL local file caption examples")
    assert len(stages) >= 3
    assert '"send_photo"' in stages[0]
    assert " OR " in stages[0]
    assert stages[0].count(" AND ") < 6
    assert '"send_photo"' in stages[1]
    assert " OR " in stages[-1]


def test_collect_query_pieces_quoted() -> None:
    phrases, tokens = _collect_query_pieces('foo "bar baz" qux')
    assert phrases == ["bar baz"]
    assert "foo" in tokens and "qux" in tokens


def test_context_line_prefers_needle_line() -> None:
    chunk = "intro\nsend_photo takes InputFile\nfooter\n"
    line = _context_line_from_chunk(chunk, "InputFile send_photo")
    assert "send_photo" in line
    assert "InputFile" in line


def test_context_line_empty_chunk() -> None:
    assert _context_line_from_chunk("", "x") == ""


def test_body_to_chunks_preserves_distant_token() -> None:
    text = ("para\n\n" * 80) + "UNIQUE_MARKER_TOKEN\n\n" + ("other\n\n" * 80)
    chunks = _body_to_chunks(text)
    assert any("UNIQUE_MARKER_TOKEN" in c for c in chunks), chunks


def test_body_to_chunks_with_lines_tracks_ranges() -> None:
    body = "alpha\n\nbeta\n\ngamma\ndelta\n"
    spans = _body_to_chunks_with_lines(body)
    assert [span.text for span in spans] == _body_to_chunks(body)
    assert spans[0].line_start == 1
    assert spans[0].line_end == 6


def test_doc_index_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "idx.sqlite"
    idx = DocIndex(db)
    url = "https://docs.example/en/page.html"
    body = "alpha\n\nbeta UNIQUEHIT gamma\n"
    idx.upsert_page(url, "Page title", body, "https://docs.example/en/", 1)

    doc = idx.get_page(url)
    assert doc is not None
    assert doc["title"] == "Page title"
    assert "UNIQUEHIT" in doc["text"]

    hits = idx.search("UNIQUEHIT", limit=5)
    assert len(hits) == 1
    assert hits[0].url == url
    assert "UNIQUEHIT" in hits[0].snippet
    assert hits[0].line == 3

    n = idx.clear_source("https://docs.example/en/")
    assert n == 1
    assert idx.get_page(url) is None
    assert idx.search("UNIQUEHIT", limit=5) == []


def test_doc_index_structured_entities_roundtrip(tmp_path: Path) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    url = "https://docs.example/api.html"
    entities = [
        {
            "local_id": "views.LayoutView",
            "parent_local_id": None,
            "anchor": "views.LayoutView",
            "kind": "class",
            "name": "LayoutView",
            "qualname": "views.LayoutView",
            "signature": "LayoutView",
            "summary": "Layout container.",
            "body_text": "Layout container.",
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
        },
        {
            "local_id": "views.LayoutView.edit_message",
            "parent_local_id": "views.LayoutView",
            "anchor": "views.LayoutView.edit_message",
            "kind": "method",
            "name": "edit_message",
            "qualname": "views.LayoutView.edit_message",
            "signature": "edit_message(text: str)",
            "summary": "Edit a message.",
            "body_text": "Edit a message.",
            "line_start": 4,
            "line_end": 8,
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
                    "text": "May fail after timeout.",
                }
            ],
        },
    ]
    idx.upsert_page(url, "API", "LayoutView\n\nedit_message(text: str)", "https://docs.example/", 1, entities)

    hits = idx.search_entities("LayoutView", kind="class", limit=5)
    assert len(hits) == 1
    assert hits[0].name == "LayoutView"

    entity = idx.get_entity(hits[0].entity_id)
    assert entity is not None
    assert entity["notes"][0]["kind"] == "versionadded"
    assert [m["name"] for m in entity["methods"]] == ["edit_message"]

    method = idx.get_entity_context("edit_message", kind="method")
    assert method is not None
    assert method["params"][0]["name"] == "text"
    assert method["notes"][0]["kind"] == "warning"

    methods = idx.list_class_methods(class_name="LayoutView")
    assert [m["name"] for m in methods] == ["edit_message"]

    idx.upsert_page(
        url,
        "API",
        "LayoutView only",
        "https://docs.example/",
        2,
        entities[:1],
    )
    assert idx.search_entities("edit_message", kind="method", limit=5) == []

    assert idx.clear_source("https://docs.example/") == 1
    assert idx.search_entities("LayoutView", kind="class", limit=5) == []


def test_doc_index_update_replaces_chunks(tmp_path: Path) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    url = "https://x/doc"
    idx.upsert_page(url, "T", "first version ZZZ", "https://x/", 1)
    assert len(idx.search("ZZZ", limit=3)) == 1
    idx.upsert_page(url, "T", "second no marker here", "https://x/", 2)
    assert idx.search("ZZZ", limit=3) == []
    hits = idx.search("second", limit=3)
    assert len(hits) == 1


def test_doc_index_search_estimates_line_for_flattened_single_block(
    tmp_path: Path,
) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    url = "https://x/flat"
    body_lines = [f"line {i}" for i in range(1, 20)]
    body_lines[11] = "allow_paid_broadcast marker token"
    body = "\n".join(body_lines) + "\n"
    idx.upsert_page(url, "Flat", body, "https://x/", 1)

    hits = idx.search("allow_paid_broadcast", limit=3)
    assert len(hits) == 1
    assert hits[0].line == 12
    assert "allow_paid_broadcast" in hits[0].snippet


def test_doc_index_search_relaxes_long_free_form_query(tmp_path: Path) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    url = "https://docs.example/send-photo.html"
    body = (
        "send_photo\n\n"
        "Use this method to send photos.\n"
        "The photo argument can be a file_id, an HTTP URL, or an InputFile.\n"
    )
    idx.upsert_page(url, "send_photo", body, "https://docs.example/", 1)

    hits = idx.search("send_photo photo URL local file caption examples", limit=5)
    assert len(hits) == 1
    assert hits[0].url == url


def test_list_sources(tmp_path: Path) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    idx.upsert_page("https://a/p", "A", "body", "https://a/", 10)
    sources = idx.list_sources()
    assert len(sources) == 1
    assert sources[0]["source_base"] == "https://a/"
    assert sources[0]["page_count"] == 1


def test_stats_json(tmp_path: Path) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    s = idx.stats_json()
    assert "db_path" in s
    assert "sources" in s


def test_list_pages_filter_and_pagination(tmp_path: Path) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    idx.upsert_page("https://a/en/p1", "P1", "b", "https://a/en/", 1)
    idx.upsert_page("https://a/en/p2", "P2", "b", "https://a/en/", 1)
    idx.upsert_page("https://b/en/x", "X", "b", "https://b/en/", 1)

    pages, total = idx.list_pages(source_base="https://a/en/")
    assert total == 2
    assert {p["url"] for p in pages} == {"https://a/en/p1", "https://a/en/p2"}

    pages2, total2 = idx.list_pages(url_contains="p1")
    assert total2 == 1
    assert pages2[0]["url"] == "https://a/en/p1"

    p3, t3 = idx.list_pages(limit=1, offset=0)
    assert t3 == 3
    assert len(p3) == 1


def test_search_source_base_scopes_results(tmp_path: Path) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    idx.upsert_page(
        "https://a/en/z.html",
        "A",
        "ONLY_A_TOKEN_HERE",
        "https://a/en/",
        1,
    )
    idx.upsert_page(
        "https://b/en/z.html",
        "B",
        "ONLY_B_TOKEN_HERE",
        "https://b/en/",
        1,
    )
    hits_all = idx.search("ONLY_A_TOKEN_HERE", limit=5)
    assert len(hits_all) == 1
    hits_b = idx.search("ONLY_A_TOKEN_HERE", limit=5, source_base="https://b/en/")
    assert hits_b == []
    hits_a = idx.search("ONLY_A_TOKEN_HERE", limit=5, source_base="https://a/en/")
    assert len(hits_a) == 1
    assert hits_a[0].url == "https://a/en/z.html"


def test_search_in_page_multiple_chunks(tmp_path: Path) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    url = "https://docs.example/page.html"
    body = (
        "MATCHRARE\n\n"
        + ("x" * 3000)
        + "\n\nMIDDLE\n\n"
        + ("y" * 3000)
        + "\n\nMATCHRARE\n"
    )
    idx.upsert_page(url, "Big", body, "https://docs.example/", 1)
    hits = idx.search_in_page(url, "MATCHRARE", limit=10)
    assert len(hits) >= 2
    assert all(h.url == url for h in hits)
    global_hits = idx.search("MATCHRARE", limit=10)
    assert len(global_hits) == 1
