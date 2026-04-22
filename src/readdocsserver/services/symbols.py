"""Symbol lookup and graph APIs."""

from __future__ import annotations

from typing import Literal

from readdocsserver.schemas.mcp import (
    LookupSymbolResponse,
    RelatedSymbolsResponse,
    SymbolGraphStatsResponse,
    SymbolLookupResult,
)
from readdocsserver.services.deps import get_doc_index
from readdocsserver.utils.validators import (
    optional_edge_types,
    optional_source_base,
    validate_limit,
    validate_symbol_name,
)


async def lookup_symbol(
    source_base: str,
    symbol_name: str,
    include_related: bool = False,
) -> LookupSymbolResponse:
    idx = get_doc_index()
    result = idx.lookup_symbol(
        optional_source_base(source_base) or "",
        validate_symbol_name(symbol_name),
        include_related=include_related,
    )
    return LookupSymbolResponse(result=SymbolLookupResult.model_validate(result))


async def related_symbols(
    source_base: str,
    symbol_name: str,
    edge_types: list[str] | None = None,
    direction: Literal["out", "in", "both"] = "both",
    limit: int = 50,
) -> RelatedSymbolsResponse:
    idx = get_doc_index()
    result = idx.related_symbols(
        optional_source_base(source_base) or "",
        validate_symbol_name(symbol_name),
        edge_types=optional_edge_types(edge_types),
        direction=direction,
        limit=validate_limit(limit),
    )
    return RelatedSymbolsResponse.model_validate(result)


async def get_symbol_graph_stats(source_base: str) -> SymbolGraphStatsResponse:
    idx = get_doc_index()
    result = idx.symbol_graph_stats(optional_source_base(source_base) or "")
    return SymbolGraphStatsResponse.model_validate(result)
