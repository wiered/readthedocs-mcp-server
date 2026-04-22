"""List indexed documentation roots."""

from __future__ import annotations

from readdocsserver.schemas.mcp import IndexedSource, SourceListResponse
from readdocsserver.services.deps import get_doc_index
from readdocsserver.store import default_db_path


async def list_indexed_sources() -> SourceListResponse:
    idx = get_doc_index()
    return SourceListResponse(
        sources=[IndexedSource.model_validate(item) for item in idx.list_sources()],
        db_path=str(idx.db_path),
        default_db_hint=str(default_db_path()),
    )
