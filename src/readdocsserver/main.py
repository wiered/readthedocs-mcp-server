"""FastMCP server for indexing and querying Read the Docs / Sphinx sites."""

from __future__ import annotations

import json
import sys
from typing import Literal

from mcp.server.fastmcp import FastMCP

from readdocsserver.mcp_instructions import INSTRUCTIONS
from readdocsserver.schemas.mcp import (
    EntityDetailResponse,
    EntityMethodListResponse,
    EntitySearchResponse,
    FetchResponse,
    IndexStats,
    ListPagesResponse,
    LookupSymbolResponse,
    RelatedSymbolsResponse,
    SearchResponse,
    SourceListResponse,
    SymbolGraphStatsResponse,
)
from readdocsserver.services import entities, indexing, pages, sources, symbols
from readdocsserver.services import fetch as fetch_service
from readdocsserver.services.search import (
    search_docs,
    search_in_file as search_in_file_page,
)
from readdocsserver.utils.env import get_mcp_transport, mcp_bind_host, mcp_bind_port
from readdocsserver.utils.validators import validate_seed_url


def create_server() -> FastMCP:
    """Build the FastMCP server with tools, prompts, and resources."""
    mcp = FastMCP(
        name="readthedocs-docs",
        instructions=INSTRUCTIONS,
        host=mcp_bind_host(),
        port=mcp_bind_port(),
    )

    @mcp.tool()
    async def index_readthedocs(
        seed_url: str,
        max_pages: int = 200,
        request_delay_sec: float = 0.15,
        replace_source: bool = True,
    ) -> IndexStats:
        """Download and index HTML pages under a Read the Docs / Sphinx doc tree."""
        return await indexing.run_index_readthedocs(
            seed_url,
            max_pages=max_pages,
            request_delay_sec=request_delay_sec,
            replace_source=replace_source,
        )

    @mcp.tool()
    async def search(
        query: str,
        limit: int = 15,
        source_base: str | None = None,
    ) -> SearchResponse:
        """Search indexed docs (at most one hit per page). Optional source_base limits results to one indexed docs root."""
        return await search_docs(query, limit=limit, source_base=source_base)

    @mcp.tool()
    async def search_entities(
        query: str,
        kind: str | None = None,
        source_base: str | None = None,
        limit: int = 15,
        include_related: bool = False,
    ) -> EntitySearchResponse:
        """Search structured docs entities such as Sphinx classes and methods."""
        return await entities.search_entities(
            query,
            kind=kind,
            source_base=source_base,
            limit=limit,
            include_related=include_related,
        )

    @mcp.tool()
    async def lookup_symbol(
        source_base: str,
        symbol_name: str,
        include_related: bool = False,
    ) -> LookupSymbolResponse:
        """Find one structured Sphinx/Python symbol and return page, anchor, lines, and short context."""
        return await symbols.lookup_symbol(
            source_base, symbol_name, include_related=include_related
        )

    @mcp.tool()
    async def related_symbols(
        source_base: str,
        symbol_name: str,
        edge_types: list[str] | None = None,
        direction: Literal["out", "in", "both"] = "both",
        limit: int = 50,
    ) -> RelatedSymbolsResponse:
        """Return typed symbol graph neighbors such as methods, return types, parameter types, and bases."""
        return await symbols.related_symbols(
            source_base,
            symbol_name,
            edge_types=edge_types,
            direction=direction,
            limit=limit,
        )

    @mcp.tool()
    async def get_symbol_graph_stats(source_base: str) -> SymbolGraphStatsResponse:
        """Return diagnostic counts for symbol graph edges and unresolved links."""
        return await symbols.get_symbol_graph_stats(source_base)

    @mcp.tool()
    async def get_entity(
        entity_id: str,
        include_methods: bool = True,
        include_params: bool = True,
        include_notes: bool = True,
    ) -> EntityDetailResponse:
        """Fetch one structured entity by entity_id from search_entities."""
        return await entities.get_entity(
            entity_id,
            include_methods=include_methods,
            include_params=include_params,
            include_notes=include_notes,
        )

    @mcp.tool()
    async def list_class_methods(
        class_name: str | None = None,
        class_entity_id: str | None = None,
        source_base: str | None = None,
    ) -> EntityMethodListResponse:
        """List methods for a class by class entity_id or class name."""
        return await entities.list_class_methods(
            class_name=class_name,
            class_entity_id=class_entity_id,
            source_base=source_base,
        )

    @mcp.tool()
    async def get_entity_context(
        query: str,
        kind: str | None = None,
        include_notes: bool = True,
        source_base: str | None = None,
    ) -> EntityDetailResponse:
        """Search one entity and return its structured params and notes context."""
        return await entities.get_entity_context(
            query,
            kind=kind,
            include_notes=include_notes,
            source_base=source_base,
        )

    @mcp.tool()
    async def search_in_file(
        page_url: str,
        query: str,
        limit: int = 15,
    ) -> SearchResponse:
        """Search inside one indexed page URL; may return several hits (different chunks) from the same file."""
        return await search_in_file_page(page_url, query, limit=limit)

    @mcp.tool()
    async def list_documentation_pages(
        source_base: str | None = None,
        url_contains: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> ListPagesResponse:
        """List indexed page URLs, titles, and section TOCs; filter by documentation root and/or URL substring."""
        return await pages.list_documentation_pages(
            source_base=source_base,
            url_contains=url_contains,
            limit=limit,
            offset=offset,
        )

    @mcp.tool()
    async def fetch(
        id: str,  # noqa: A002
        start: int | None = None,
        end: int | None = None,
    ) -> FetchResponse:
        """Load one indexed page by id (URL from `search`). Optional start/end: 1-based inclusive line numbers."""
        return await fetch_service.fetch_page(id, start=start, end=end)

    @mcp.tool()
    async def list_indexed_sources() -> SourceListResponse:
        """List documentation roots currently stored and page counts."""
        return await sources.list_indexed_sources()

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
        validated_seed_url = validate_seed_url(seed_url)
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
    mcp.run(transport=get_mcp_transport())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
