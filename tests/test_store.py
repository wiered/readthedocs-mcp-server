"""Tests for readdocsserver.store (FTS index, chunking, search)."""

from __future__ import annotations

from pathlib import Path

import pytest

from readdocsserver.store import (
    DocIndex,
    _body_to_chunks,
    _collect_query_pieces,
    _context_line_from_chunk,
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

    n = idx.clear_source("https://docs.example/en/")
    assert n == 1
    assert idx.get_page(url) is None
    assert idx.search("UNIQUEHIT", limit=5) == []


def test_doc_index_update_replaces_chunks(tmp_path: Path) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    url = "https://x/doc"
    idx.upsert_page(url, "T", "first version ZZZ", "https://x/", 1)
    assert len(idx.search("ZZZ", limit=3)) == 1
    idx.upsert_page(url, "T", "second no marker here", "https://x/", 2)
    assert idx.search("ZZZ", limit=3) == []
    hits = idx.search("second", limit=3)
    assert len(hits) == 1


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
