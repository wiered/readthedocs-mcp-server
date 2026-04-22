"""Pydantic models for MCP tool and resource responses."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class IndexedSource(BaseModel):
    """One indexed documentation root stored in SQLite."""

    source_base: str
    page_count: int
    last_fetched_at: int | None


class IndexStats(BaseModel):
    """Crawler summary returned after indexing a docs site."""

    source_base: str
    fetched: int
    skipped: int
    sitemap_seeds: int = 0
    errors: list[dict[str, str]] = Field(default_factory=list)
    db_path: str


class SearchResult(BaseModel):
    """Search hit with one approximate matching line and its approximate line number."""

    id: str
    title: str
    text: str
    url: str
    line: int | None = None
    chunk_line_start: int | None = None
    chunk_line_end: int | None = None


class SearchResponse(BaseModel):
    """Collection of search hits."""

    results: list[SearchResult]


class EntityParam(BaseModel):
    """One documented parameter for a structured API entity."""

    ord: int
    name: str
    type: str = ""
    default: str = ""
    description: str = ""


class EntityNote(BaseModel):
    """A note, warning, admonition, or version marker attached to an entity."""

    ord: int
    kind: str
    version: str = ""
    text: str


class EntityResult(BaseModel):
    """Search result for a structured documentation entity."""

    entity_id: str
    kind: str
    name: str
    qualname: str
    signature: str
    summary: str
    page_url: str
    anchor: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    parent_entity_id: str | None = None
    related: dict[str, Any] | None = None


class EntitySearchResponse(BaseModel):
    """Collection of structured entity search hits."""

    results: list[EntityResult]


class EntityDetail(EntityResult):
    """Full structured entity payload."""

    source_base: str
    body_text: str
    params: list[EntityParam] = Field(default_factory=list)
    notes: list[EntityNote] = Field(default_factory=list)
    methods: list[EntityDetail] = Field(default_factory=list)


class EntityDetailResponse(BaseModel):
    """One structured entity, or a not-found marker."""

    entity: EntityDetail | None


class EntityMethodListResponse(BaseModel):
    """Methods belonging to one class entity."""

    methods: list[EntityDetail]


class SymbolLookupResult(BaseModel):
    """One best structured symbol lookup result with page location context."""

    found: bool
    symbol_name: str
    page_url: str | None = None
    anchor: str | None = None
    url_with_anchor: str | None = None
    kind: str | None = None
    name: str | None = None
    qualname: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    context_start: int | None = None
    context_end: int | None = None
    context: str = ""
    summary: str = ""
    entity_id: str | None = None
    related: dict[str, Any] | None = None


class LookupSymbolResponse(BaseModel):
    """Symbol lookup response."""

    result: SymbolLookupResult


class RelatedSymbolRef(BaseModel):
    """Compact entity reference used in symbol graph responses."""

    entity_id: str
    qualname: str
    kind: str
    page_url: str
    anchor: str | None = None


class RelatedSymbolEdge(BaseModel):
    """One typed edge adjacent to a symbol."""

    direction: Literal["out", "in"]
    edge_type: str
    source_kind: str
    param_name: str = ""
    confidence: float
    snippet: str = ""
    source_page_url: str = ""
    line_start: int | None = None
    line_end: int | None = None
    relation_label: str = ""
    target: RelatedSymbolRef


class RelatedSymbolsResponse(BaseModel):
    """Typed symbol graph neighbors for one entity."""

    found: bool
    symbol: RelatedSymbolRef | None = None
    edges: list[RelatedSymbolEdge] = Field(default_factory=list)


class SymbolGraphStatsResponse(BaseModel):
    """Diagnostic statistics for the stored symbol graph."""

    source_base: str
    edge_type_counts: dict[str, int] = Field(default_factory=dict)
    source_kind_counts: dict[str, int] = Field(default_factory=dict)
    unresolved_xref_target_count: int
    top_unresolved_targets: list[dict[str, Any]] = Field(default_factory=list)
    stale_edge_count: int
    total_entities: int
    total_pages: int


class FetchMetadata(BaseModel):
    """Source metadata attached to a fetched page."""

    source_base: str
    fetched_at: int


class FetchResponse(BaseModel):
    """Indexed page payload (full body or a 1-based inclusive line range)."""

    id: str
    title: str
    text: str
    url: str
    metadata: FetchMetadata | None
    total_lines: int | None = None
    slice_start: int | None = None
    slice_end: int | None = None


class SourceListResponse(BaseModel):
    """Indexed source overview."""

    sources: list[IndexedSource]
    db_path: str
    default_db_hint: str


class ListedPage(BaseModel):
    """One indexed documentation page (URL inventory for list_documentation_pages)."""

    url: str
    title: str
    source_base: str


class ListPagesResponse(BaseModel):
    """Paginated list of indexed pages."""

    pages: list[ListedPage]
    total: int
    limit: int
    offset: int
    db_path: str


EntityDetail.model_rebuild()
