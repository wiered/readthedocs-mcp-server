"""Full-text search over indexed pages."""

from __future__ import annotations

from readdocsserver.schemas.mcp import SearchResponse, SearchResult
from readdocsserver.services.deps import get_doc_index
from readdocsserver.utils.validators import (
    optional_source_base,
    validate_limit,
    validate_page_url,
)


async def search_docs(
    query: str,
    limit: int = 15,
    source_base: str | None = None,
) -> SearchResponse:
    stripped_query = query.strip()
    if not stripped_query:
        raise ValueError("query must not be empty")

    idx = get_doc_index()
    hits = idx.search(
        stripped_query,
        limit=validate_limit(limit),
        source_base=optional_source_base(source_base),
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


async def search_in_file(
    page_url: str,
    query: str,
    limit: int = 15,
) -> SearchResponse:
    stripped_query = query.strip()
    if not stripped_query:
        raise ValueError("query must not be empty")
    validated_url = validate_page_url(page_url)

    idx = get_doc_index()
    hits = idx.search_in_page(
        validated_url, stripped_query, limit=validate_limit(limit)
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
