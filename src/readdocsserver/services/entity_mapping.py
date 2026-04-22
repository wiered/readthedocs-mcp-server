"""Map DocIndex rows and dict payloads to MCP Pydantic models."""

from __future__ import annotations

from typing import Any

from readdocsserver.schemas.mcp import (
    EntityDetail,
    EntityNote,
    EntityParam,
    EntityResult,
)
from readdocsserver.store import DocIndex


def entity_result_from_hit(hit: Any) -> EntityResult:
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


def entity_result_from_hit_with_related(
    idx: DocIndex, hit: Any, source_base: str | None
) -> EntityResult:
    result = entity_result_from_hit(hit)
    if source_base:
        related = idx.related_symbols(
            source_base,
            hit.qualname,
            direction="both",
            limit=30,
        )
        if related.get("found"):
            result.related = {
                "out": [
                    edge
                    for edge in related.get("edges", [])
                    if edge.get("direction") == "out"
                ],
                "in": [
                    edge
                    for edge in related.get("edges", [])
                    if edge.get("direction") == "in"
                ],
            }
    return result


def entity_detail_from_dict(data: dict) -> EntityDetail:
    methods = [
        entity_detail_from_dict(method)
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
