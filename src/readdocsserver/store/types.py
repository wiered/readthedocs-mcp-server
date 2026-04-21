"""Dataclasses for chunk spans and search / entity hits."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkSpan:
    text: str
    line_start: int
    line_end: int


@dataclass
class SearchHit:
    url: str
    title: str
    snippet: str
    rank: float
    line: int | None = None
    chunk_line_start: int | None = None
    chunk_line_end: int | None = None


@dataclass
class EntityHit:
    entity_id: str
    kind: str
    name: str
    qualname: str
    signature: str
    summary: str
    page_url: str
    anchor: str | None
    rank: float
    line_start: int | None = None
    line_end: int | None = None
    parent_entity_id: str | None = None
