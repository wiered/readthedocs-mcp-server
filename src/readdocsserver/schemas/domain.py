"""Domain dataclasses for indexing, search hits, and symbol resolution."""

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


@dataclass
class EntityEdge:
    edge_id: int | None
    source_base: str
    from_entity_id: str
    to_entity_id: str
    edge_type: str
    source_kind: str
    param_name: str = ""
    confidence: float = 1.0
    snippet: str = ""
    page_url: str = ""
    line_start: int | None = None
    line_end: int | None = None


@dataclass
class EntityXrefCandidate:
    source_base: str
    page_url: str
    from_entity_id: str
    edge_type: str
    source_kind: str
    target_url: str = ""
    target_anchor: str = ""
    target_name: str = ""
    snippet: str = ""
    confidence: float = 1.0
    line_start: int | None = None
    line_end: int | None = None


@dataclass(frozen=True)
class SymbolInfo:
    entity_id: str
    name: str
    qualname: str


@dataclass(frozen=True)
class SymbolMaps:
    by_qualname: dict[str, str]
    by_unique_name: dict[str, str]
    symbols: dict[str, SymbolInfo]
