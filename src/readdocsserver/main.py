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
    """One indexed documentation page (URL + title + root)."""

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


def _validate_list_pages_limit(limit: int) -> int:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    return limit


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
    return s or None


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
        ) -> None:
            idx.upsert_page(url, title, body, source_base, fetched_at)

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
        """List indexed page URLs and titles; filter by documentation root (source_base) and/or URL substring."""
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
