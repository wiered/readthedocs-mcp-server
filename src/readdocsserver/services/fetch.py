"""Fetch one indexed page by URL id, optionally as a line slice."""

from __future__ import annotations

from readdocsserver.schemas.mcp import FetchMetadata, FetchResponse
from readdocsserver.services.deps import get_doc_index
from readdocsserver.utils.text import slice_body_lines
from readdocsserver.utils.validators import validate_line_slice


async def fetch_page(
    id: str,  # noqa: A002
    start: int | None = None,
    end: int | None = None,
) -> FetchResponse:
    page_id = id.strip()
    if not page_id:
        raise ValueError("id must not be empty")
    span = validate_line_slice(start, end)

    idx = get_doc_index()
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
    slice_text, n_lines, s_use, e_use = slice_body_lines(body, s, e)
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
