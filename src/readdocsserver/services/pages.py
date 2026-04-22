"""List indexed pages with optional filters."""

from __future__ import annotations

from readdocsserver.schemas.mcp import ListPagesResponse, ListedPage
from readdocsserver.services.deps import get_doc_index
from readdocsserver.utils.validators import (
    optional_source_base,
    optional_url_contains,
    validate_list_pages_limit,
    validate_offset,
)


async def list_documentation_pages(
    source_base: str | None = None,
    url_contains: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> ListPagesResponse:
    lim = validate_list_pages_limit(limit)
    off = validate_offset(offset)
    idx = get_doc_index()
    pages, total = idx.list_pages(
        source_base=optional_source_base(source_base),
        url_contains=optional_url_contains(url_contains),
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
