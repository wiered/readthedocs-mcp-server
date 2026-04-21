"""FastMCP server for indexing and querying Read the Docs / Sphinx sites."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from readdocsserver.crawl import crawl_readthedocs, normalize_doc_root
from readdocsserver.store import DocIndex, default_db_path

INSTRUCTIONS = """
Index Read the Docs / Sphinx HTML locally and query it with MCP search/fetch tools.

Available MCP capabilities:
- Tools for indexing, searching, fetching, listing indexed sources, listing pages per root, and scoped search (whole source or single page).
- Symbol lookup for structured Sphinx/Python classes and methods when entity data is available.
- Related symbol lookup for typed graph neighbors such as class methods, return types, parameter types, and base classes.
- Prompts exposed as slash commands in compatible MCP clients for common workflows.
- A status resource with the current database path and indexed source summary.

Environment:
- READTHEDOCS_MCP_DB: optional SQLite index path (default: ~/.cache/readdocs-mcp/index.sqlite).
- READTHEDOCS_MCP_IMPERSONATE: optional curl_cffi browser TLS profile (default: chrome).
- READTHEDOCS_MCP_TRANSPORT: stdio (default), sse, or streamable-http.
- READTHEDOCS_MCP_HOST / READTHEDOCS_MCP_PORT: bind address for HTTP transports.
""".strip()


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


class EntitySearchResponse(BaseModel):
    """Collection of structured entity search hits."""

    results: list[EntityResult]


class EntityDetail(EntityResult):
    """Full structured entity payload."""

    source_base: str
    body_text: str
    params: list[EntityParam] = Field(default_factory=list)
    notes: list[EntityNote] = Field(default_factory=list)
    methods: list["EntityDetail"] = Field(default_factory=list)


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
    target: RelatedSymbolRef


class RelatedSymbolsResponse(BaseModel):
    """Typed symbol graph neighbors for one entity."""

    found: bool
    symbol: RelatedSymbolRef | None = None
    edges: list[RelatedSymbolEdge] = Field(default_factory=list)


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


class PageSection(BaseModel):
    """One section in a page-level table of contents."""

    id: str
    title: str
    summary: str = ""
    level: int
    url: str
    children: list["PageSection"] = Field(default_factory=list)


class ListedPage(BaseModel):
    """One indexed documentation page with a lightweight section overview."""

    url: str
    title: str
    source_base: str
    toc: list[PageSection] = Field(default_factory=list)


class ListPagesResponse(BaseModel):
    """Paginated list of indexed pages."""

    pages: list[ListedPage]
    total: int
    limit: int
    offset: int
    db_path: str


def _index() -> DocIndex:
    path = os.environ.get("READTHEDOCS_MCP_DB")
    return DocIndex(Path(path) if path else None)


def _mcp_bind_host() -> str:
    return os.environ.get("READTHEDOCS_MCP_HOST", "127.0.0.1")


def _mcp_bind_port() -> int:
    return int(os.environ.get("READTHEDOCS_MCP_PORT", "8000"))


def _transport() -> Literal["stdio", "sse", "streamable-http"]:
    raw = os.environ.get("READTHEDOCS_MCP_TRANSPORT", "stdio").strip().lower()
    if raw in {"sse", "streamable-http"}:
        return raw
    return "stdio"


def _validate_seed_url(seed_url: str) -> str:
    normalized = seed_url.strip()
    if not normalized:
        raise ValueError("seed_url must not be empty")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("seed_url must be an absolute http(s) URL")
    return normalized


def _validate_max_pages(max_pages: int) -> int:
    if not 1 <= max_pages <= 5000:
        raise ValueError("max_pages must be between 1 and 5000")
    return max_pages


def _validate_delay(request_delay_sec: float) -> float:
    if not 0 <= request_delay_sec <= 30:
        raise ValueError("request_delay_sec must be between 0 and 30")
    return request_delay_sec


def _validate_limit(limit: int) -> int:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    return limit


def _optional_entity_kind(kind: str | None) -> str | None:
    if kind is None:
        return None
    k = kind.strip().lower()
    if not k:
        return None
    if k not in {"class", "method"}:
        raise ValueError("kind must be 'class', 'method', or omitted")
    return k


def _validate_symbol_name(symbol_name: str) -> str:
    s = symbol_name.strip()
    if not s:
        raise ValueError("symbol_name must not be empty")
    return s


def _validate_list_pages_limit(limit: int) -> int:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    return limit


_VALID_EDGE_TYPES = {
    "has_method",
    "inherits_from",
    "returns",
    "accepts_parameter_type",
    "references",
}


def _optional_edge_types(edge_types: list[str] | None) -> list[str] | None:
    if edge_types is None:
        return None
    cleaned: list[str] = []
    for edge_type in edge_types:
        value = edge_type.strip()
        if not value:
            continue
        if value not in _VALID_EDGE_TYPES:
            raise ValueError(f"Unsupported edge_type: {value}")
        if value not in cleaned:
            cleaned.append(value)
    return cleaned or None


def _validate_offset(offset: int) -> int:
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if offset > 1_000_000:
        raise ValueError("offset is too large")
    return offset


def _optional_source_base(source_base: str | None) -> str | None:
    if source_base is None:
        return None
    s = source_base.strip()
    if not s:
        return None
    # Match crawler-stored roots (see crawl_readthedocs: always normalize_doc_root(seed)).
    # Also accepts a concrete page URL and narrows it to the docs directory prefix.
    return normalize_doc_root(s)


def _optional_url_contains(url_contains: str | None) -> str | None:
    if url_contains is None:
        return None
    s = url_contains.strip()
    return s or None


def _validate_page_url(page_url: str) -> str:
    u = page_url.strip()
    if not u:
        raise ValueError("page_url must not be empty")
    parsed = urlparse(u)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("page_url must be an absolute http(s) URL")
    return u


_MAX_FETCH_LINES = 5000


def _validate_line_slice(start: int | None, end: int | None) -> tuple[int, int] | None:
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise ValueError(
            "Provide both start and end (1-based inclusive line numbers), or omit both for the full page."
        )
    if start < 1 or end < start:
        raise ValueError("start must be >= 1 and end must be >= start.")
    if end - start + 1 > _MAX_FETCH_LINES:
        raise ValueError(f"At most {_MAX_FETCH_LINES} lines per fetch.")
    return (start, end)


def _slice_body_lines(body: str, start: int, end: int) -> tuple[str, int, int, int]:
    """Return (slice_text, total_lines, slice_start_used, slice_end_used)."""
    lines = body.splitlines()
    n = len(lines)
    if n == 0:
        return "", 0, start, end
    if start > n:
        return "", n, start, min(end, n)
    s = max(1, start)
    e = min(max(s, end), n)
    return "\n".join(lines[s - 1 : e]), n, s, e


def _entity_result_from_hit(hit) -> EntityResult:
    return EntityResult(
        entity_id=hit.entity_id,
        kind=hit.kind,
        name=hit.name,
        qualname=hit.qualname,
        signature=hit.signature,
        summary=hit.summary,
        page_url=hit.page_url,
        anchor=hit.anchor,
        line_start=hit.line_start,
        line_end=hit.line_end,
        parent_entity_id=hit.parent_entity_id,
    )


def _entity_detail_from_dict(data: dict) -> EntityDetail:
    methods = [
        _entity_detail_from_dict(method)
        for method in data.get("methods", [])
        if isinstance(method, dict)
    ]
    return EntityDetail(
        entity_id=str(data["entity_id"]),
        source_base=str(data["source_base"]),
        page_url=str(data["page_url"]),
        anchor=data.get("anchor"),
        kind=str(data["kind"]),
        name=str(data["name"]),
        qualname=str(data["qualname"]),
        signature=str(data["signature"]),
        summary=str(data["summary"]),
        body_text=str(data["body_text"]),
        line_start=data.get("line_start"),
        line_end=data.get("line_end"),
        parent_entity_id=data.get("parent_entity_id"),
        params=[EntityParam.model_validate(p) for p in data.get("params", [])],
        notes=[EntityNote.model_validate(n) for n in data.get("notes", [])],
        methods=methods,
    )


def create_server() -> FastMCP:
    """Build the FastMCP server with tools, prompts, and resources."""
    mcp = FastMCP(
        name="readthedocs-docs",
        instructions=INSTRUCTIONS,
        host=_mcp_bind_host(),
        port=_mcp_bind_port(),
    )

    @mcp.tool()
    async def index_readthedocs(
        seed_url: str,
        max_pages: int = 200,
        request_delay_sec: float = 0.15,
        replace_source: bool = True,
    ) -> IndexStats:
        """Download and index HTML pages under a Read the Docs / Sphinx doc tree."""
        validated_seed_url = _validate_seed_url(seed_url)
        validated_max_pages = _validate_max_pages(max_pages)
        validated_delay = _validate_delay(request_delay_sec)

        idx = _index()
        root = normalize_doc_root(validated_seed_url)
        if replace_source:
            idx.clear_source(root)

        async def on_page(
            url: str,
            title: str,
            body: str,
            source_base: str,
            fetched_at: int,
            entities: list[dict],
            toc: list[dict],
        ) -> None:
            idx.upsert_page(url, title, body, source_base, fetched_at, entities, toc)

        stats = await crawl_readthedocs(
            validated_seed_url,
            max_pages=validated_max_pages,
            request_delay_sec=validated_delay,
            on_page=on_page,
        )
        return IndexStats.model_validate({**stats, "db_path": str(idx.db_path)})

    @mcp.tool()
    async def search(
        query: str,
        limit: int = 15,
        source_base: str | None = None,
    ) -> SearchResponse:
        """Search indexed docs (at most one hit per page). Optional source_base limits results to one indexed docs root."""
        stripped_query = query.strip()
        if not stripped_query:
            raise ValueError("query must not be empty")

        idx = _index()
        hits = idx.search(
            stripped_query,
            limit=_validate_limit(limit),
            source_base=_optional_source_base(source_base),
        )
        results = [
            SearchResult(
                id=hit.url,
                title=hit.title,
                text=hit.snippet,
                url=hit.url,
                line=hit.line,
                chunk_line_start=hit.chunk_line_start,
                chunk_line_end=hit.chunk_line_end,
            )
            for hit in hits
        ]
        return SearchResponse(results=results)

    @mcp.tool()
    async def search_entities(
        query: str,
        kind: str | None = None,
        source_base: str | None = None,
        limit: int = 15,
    ) -> EntitySearchResponse:
        """Search structured docs entities such as Sphinx classes and methods."""
        stripped_query = query.strip()
        if not stripped_query:
            raise ValueError("query must not be empty")
        idx = _index()
        hits = idx.search_entities(
            stripped_query,
            limit=_validate_limit(limit),
            kind=_optional_entity_kind(kind),
            source_base=_optional_source_base(source_base),
        )
        return EntitySearchResponse(
            results=[_entity_result_from_hit(hit) for hit in hits]
        )

    @mcp.tool()
    async def lookup_symbol(
        source_base: str,
        symbol_name: str,
    ) -> LookupSymbolResponse:
        """Find one structured Sphinx/Python symbol and return page, anchor, lines, and short context."""
        idx = _index()
        result = idx.lookup_symbol(
            _optional_source_base(source_base) or "",
            _validate_symbol_name(symbol_name),
        )
        return LookupSymbolResponse(result=SymbolLookupResult.model_validate(result))

    @mcp.tool()
    async def related_symbols(
        source_base: str,
        symbol_name: str,
        edge_types: list[str] | None = None,
        direction: Literal["out", "in", "both"] = "both",
        limit: int = 50,
    ) -> RelatedSymbolsResponse:
        """Return typed symbol graph neighbors such as methods, return types, parameter types, and bases."""
        idx = _index()
        result = idx.related_symbols(
            _optional_source_base(source_base) or "",
            _validate_symbol_name(symbol_name),
            edge_types=_optional_edge_types(edge_types),
            direction=direction,
            limit=_validate_limit(limit),
        )
        return RelatedSymbolsResponse.model_validate(result)

    @mcp.tool()
    async def get_entity(
        entity_id: str,
        include_methods: bool = True,
        include_params: bool = True,
        include_notes: bool = True,
    ) -> EntityDetailResponse:
        """Fetch one structured entity by entity_id from search_entities."""
        eid = entity_id.strip()
        if not eid:
            raise ValueError("entity_id must not be empty")
        idx = _index()
        entity = idx.get_entity(
            eid,
            include_methods=include_methods,
            include_params=include_params,
            include_notes=include_notes,
        )
        return EntityDetailResponse(
            entity=_entity_detail_from_dict(entity) if entity else None
        )

    @mcp.tool()
    async def list_class_methods(
        class_name: str | None = None,
        class_entity_id: str | None = None,
        source_base: str | None = None,
    ) -> EntityMethodListResponse:
        """List methods for a class by class entity_id or class name."""
        cleaned_name = class_name.strip() if class_name else None
        cleaned_id = class_entity_id.strip() if class_entity_id else None
        if not cleaned_name and not cleaned_id:
            raise ValueError("Provide class_name or class_entity_id.")
        idx = _index()
        methods = idx.list_class_methods(
            class_entity_id=cleaned_id,
            class_name=cleaned_name,
            source_base=_optional_source_base(source_base),
        )
        return EntityMethodListResponse(
            methods=[_entity_detail_from_dict(method) for method in methods]
        )

    @mcp.tool()
    async def get_entity_context(
        query: str,
        kind: str | None = None,
        include_notes: bool = True,
        source_base: str | None = None,
    ) -> EntityDetailResponse:
        """Search one entity and return its structured params and notes context."""
        stripped_query = query.strip()
        if not stripped_query:
            raise ValueError("query must not be empty")
        idx = _index()
        entity = idx.get_entity_context(
            stripped_query,
            kind=_optional_entity_kind(kind),
            include_notes=include_notes,
            source_base=_optional_source_base(source_base),
        )
        return EntityDetailResponse(
            entity=_entity_detail_from_dict(entity) if entity else None
        )

    @mcp.tool()
    async def search_in_file(
        page_url: str,
        query: str,
        limit: int = 15,
    ) -> SearchResponse:
        """Search inside one indexed page URL; may return several hits (different chunks) from the same file."""
        stripped_query = query.strip()
        if not stripped_query:
            raise ValueError("query must not be empty")
        validated_url = _validate_page_url(page_url)

        idx = _index()
        hits = idx.search_in_page(
            validated_url, stripped_query, limit=_validate_limit(limit)
        )
        results = [
            SearchResult(
                id=hit.url,
                title=hit.title,
                text=hit.snippet,
                url=hit.url,
                line=hit.line,
                chunk_line_start=hit.chunk_line_start,
                chunk_line_end=hit.chunk_line_end,
            )
            for hit in hits
        ]
        return SearchResponse(results=results)

    @mcp.tool()
    async def list_documentation_pages(
        source_base: str | None = None,
        url_contains: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> ListPagesResponse:
        """List indexed page URLs, titles, and section TOCs; filter by documentation root and/or URL substring."""
        lim = _validate_list_pages_limit(limit)
        off = _validate_offset(offset)
        idx = _index()
        pages, total = idx.list_pages(
            source_base=_optional_source_base(source_base),
            url_contains=_optional_url_contains(url_contains),
            limit=lim,
            offset=off,
        )
        return ListPagesResponse(
            pages=[ListedPage.model_validate(p) for p in pages],
            total=total,
            limit=lim,
            offset=off,
            db_path=str(idx.db_path),
        )

    @mcp.tool()
    async def fetch(
        id: str,  # noqa: A002
        start: int | None = None,
        end: int | None = None,
    ) -> FetchResponse:
        """Load one indexed page by id (URL from `search`). Optional start/end: 1-based inclusive line numbers."""
        page_id = id.strip()
        if not page_id:
            raise ValueError("id must not be empty")
        span = _validate_line_slice(start, end)

        idx = _index()
        doc = idx.get_page(page_id)
        if doc is None:
            return FetchResponse(
                id=page_id,
                title="Not found",
                text="No indexed page for this id. Run index_readthedocs first or check the URL.",
                url=page_id,
                metadata=None,
                total_lines=None,
                slice_start=None,
                slice_end=None,
            )

        metadata = doc.get("metadata")
        body = str(doc["text"])
        total_lines = len(body.splitlines())
        if span is None:
            return FetchResponse(
                id=str(doc["id"]),
                title=str(doc["title"]),
                text=body,
                url=str(doc["url"]),
                metadata=FetchMetadata.model_validate(metadata) if metadata else None,
                total_lines=total_lines,
                slice_start=None,
                slice_end=None,
            )
        s, e = span
        slice_text, n_lines, s_use, e_use = _slice_body_lines(body, s, e)
        return FetchResponse(
            id=str(doc["id"]),
            title=str(doc["title"]),
            text=slice_text,
            url=str(doc["url"]),
            metadata=FetchMetadata.model_validate(metadata) if metadata else None,
            total_lines=n_lines,
            slice_start=s_use,
            slice_end=e_use,
        )

    @mcp.tool()
    async def list_indexed_sources() -> SourceListResponse:
        """List documentation roots currently stored and page counts."""
        idx = _index()
        return SourceListResponse(
            sources=[IndexedSource.model_validate(item) for item in idx.list_sources()],
            db_path=str(idx.db_path),
            default_db_hint=str(default_db_path()),
        )

    @mcp.resource(
        "readdocs://status",
        name="readdocs-status",
        title="Read the Docs Index Status",
        description="Current database path and summary of indexed documentation roots.",
    )
    async def index_status() -> str:
        """Expose a compact JSON snapshot of the local docs index."""
        payload = await list_indexed_sources()
        return json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2)

    @mcp.prompt(
        name="index-docs",
        title="Index Docs",
        description="Slash command for indexing a Read the Docs or Sphinx documentation tree.",
    )
    def index_docs_prompt(seed_url: str) -> str:
        """Create a prompt that asks the assistant to index a documentation site."""
        validated_seed_url = _validate_seed_url(seed_url)
        return (
            "Index this documentation site with the `index_readthedocs` tool.\n"
            f"seed_url: {validated_seed_url}\n"
            "Use the default crawl settings unless I specify otherwise, then summarize the indexed source."
        )

    @mcp.prompt(
        name="search-docs",
        title="Search Docs",
        description="Slash command for searching the local documentation index.",
    )
    def search_docs_prompt(query: str) -> str:
        """Create a prompt that asks the assistant to search indexed docs."""
        stripped_query = query.strip()
        if not stripped_query:
            raise ValueError("query must not be empty")
        return (
            "Search the local Read the Docs index with the `search` tool.\n"
            f"query: {stripped_query}\n"
            "Optional: pass `source_base` from `list_indexed_sources` to scope to one docs root.\n"
            "Return the most relevant hits and suggest which id to fetch next."
        )

    @mcp.prompt(
        name="read-page",
        title="Read Page",
        description="Slash command for fetching an indexed page and answering from its contents.",
    )
    def read_page_prompt(id: str, question: str | None = None) -> str:  # noqa: A002
        """Create a prompt that fetches an indexed page and optionally answers a question."""
        page_id = id.strip()
        if not page_id:
            raise ValueError("id must not be empty")
        extra = (
            f"\nThen answer this question about the page: {question.strip()}"
            if question and question.strip()
            else ""
        )
        return (
            "Fetch this indexed documentation page with the `fetch` tool.\n"
            f"id: {page_id}"
            f"{extra}"
        )

    return mcp


mcp = create_server()


def main() -> None:
    """Run the MCP server with the configured transport."""
    mcp.run(transport=_transport())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
