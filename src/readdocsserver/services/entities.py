"""Structured entity search and detail APIs."""

from __future__ import annotations

from readdocsserver.schemas.mcp import (
    EntityDetailResponse,
    EntityMethodListResponse,
    EntitySearchResponse,
)
from readdocsserver.services.deps import get_doc_index
from readdocsserver.services.entity_mapping import (
    entity_detail_from_dict,
    entity_result_from_hit,
    entity_result_from_hit_with_related,
)
from readdocsserver.utils.validators import (
    optional_entity_kind,
    optional_source_base,
    validate_limit,
)


async def search_entities(
    query: str,
    kind: str | None = None,
    source_base: str | None = None,
    limit: int = 15,
    include_related: bool = False,
) -> EntitySearchResponse:
    stripped_query = query.strip()
    if not stripped_query:
        raise ValueError("query must not be empty")
    idx = get_doc_index()
    scoped_source = optional_source_base(source_base)
    hits = idx.search_entities(
        stripped_query,
        limit=validate_limit(limit),
        kind=optional_entity_kind(kind),
        source_base=scoped_source,
    )
    if include_related:
        results = [
            entity_result_from_hit_with_related(idx, hit, scoped_source) for hit in hits
        ]
    else:
        results = [entity_result_from_hit(hit) for hit in hits]
    return EntitySearchResponse(results=results)


async def get_entity(
    entity_id: str,
    include_methods: bool = True,
    include_params: bool = True,
    include_notes: bool = True,
) -> EntityDetailResponse:
    eid = entity_id.strip()
    if not eid:
        raise ValueError("entity_id must not be empty")
    idx = get_doc_index()
    entity = idx.get_entity(
        eid,
        include_methods=include_methods,
        include_params=include_params,
        include_notes=include_notes,
    )
    return EntityDetailResponse(
        entity=entity_detail_from_dict(entity) if entity else None
    )


async def list_class_methods(
    class_name: str | None = None,
    class_entity_id: str | None = None,
    source_base: str | None = None,
) -> EntityMethodListResponse:
    cleaned_name = class_name.strip() if class_name else None
    cleaned_id = class_entity_id.strip() if class_entity_id else None
    if not cleaned_name and not cleaned_id:
        raise ValueError("Provide class_name or class_entity_id.")
    idx = get_doc_index()
    methods = idx.list_class_methods(
        class_entity_id=cleaned_id,
        class_name=cleaned_name,
        source_base=optional_source_base(source_base),
    )
    return EntityMethodListResponse(
        methods=[entity_detail_from_dict(method) for method in methods]
    )


async def get_entity_context(
    query: str,
    kind: str | None = None,
    include_notes: bool = True,
    source_base: str | None = None,
) -> EntityDetailResponse:
    stripped_query = query.strip()
    if not stripped_query:
        raise ValueError("query must not be empty")
    idx = get_doc_index()
    entity = idx.get_entity_context(
        stripped_query,
        kind=optional_entity_kind(kind),
        include_notes=include_notes,
        source_base=optional_source_base(source_base),
    )
    return EntityDetailResponse(
        entity=entity_detail_from_dict(entity) if entity else None
    )
