"""SQLite FTS5 index for crawled documentation pages."""

from __future__ import annotations

from .chunking import _body_to_chunks, _body_to_chunks_with_lines
from .docindex import DocIndex
from .fts_query import (
    _collect_query_pieces,
    _fts_match_queries,
    _fts_match_stages,
)
from .paths import default_db_path
from .snippets import _context_line_from_chunk
from .types import ChunkSpan, EntityEdge, EntityHit, SearchHit

__all__ = [
    "ChunkSpan",
    "DocIndex",
    "EntityEdge",
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
