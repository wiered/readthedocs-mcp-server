"""SQLite FTS5 index for crawled documentation pages."""

from __future__ import annotations

from readdocsserver.schemas.domain import (
    ChunkSpan,
    EntityEdge,
    EntityHit,
    EntityXrefCandidate,
    SearchHit,
)
from readdocsserver.services.chunking import _body_to_chunks, _body_to_chunks_with_lines
from readdocsserver.services.fts_query import (
    _collect_query_pieces,
    _fts_match_queries,
    _fts_match_stages,
)
from readdocsserver.services.snippets import _context_line_from_chunk

from .docindex import DocIndex
from .paths import default_db_path

__all__ = [
    "ChunkSpan",
    "DocIndex",
    "EntityEdge",
    "EntityXrefCandidate",
    "EntityHit",
    "SearchHit",
    "default_db_path",
    "_body_to_chunks",
    "_body_to_chunks_with_lines",
    "_collect_query_pieces",
    "_context_line_from_chunk",
    "_fts_match_queries",
    "_fts_match_stages",
]
