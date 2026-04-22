"""Build lookup_symbol response payloads."""

from __future__ import annotations

import sqlite3
from typing import Any

from readdocsserver.services.constants import (
    _LOOKUP_CONTEXT_AFTER,
    _LOOKUP_CONTEXT_BEFORE,
)


def _empty_symbol_lookup(symbol_name: str) -> dict[str, Any]:
    return {
        "found": False,
        "symbol_name": symbol_name,
        "page_url": None,
        "anchor": None,
        "url_with_anchor": None,
        "kind": None,
        "name": None,
        "qualname": None,
        "line_start": None,
        "line_end": None,
        "context_start": None,
        "context_end": None,
        "context": "",
        "summary": "",
        "entity_id": None,
    }


def _slice_lookup_context(
    body: str, line_start: int | None, line_end: int | None
) -> tuple[str, int | None, int | None]:
    if line_start is None or line_end is None:
        return "", None, None
    lines = body.splitlines()
    if not lines:
        return "", None, None
    start = max(1, line_start - _LOOKUP_CONTEXT_BEFORE)
    end = min(len(lines), max(line_start, line_end) + _LOOKUP_CONTEXT_AFTER)
    return "\n".join(lines[start - 1 : end]), start, end


def _symbol_lookup_from_row(row: sqlite3.Row, page_body: str | None) -> dict[str, Any]:
    line_start = row["line_start"]
    line_end = row["line_end"]
    context, context_start, context_end = _slice_lookup_context(
        page_body or "", line_start, line_end
    )
    if not context:
        context = str(row["body_text"] or row["summary"] or "")
        context_start = None
        context_end = None
    anchor = row["anchor"]
    page_url = str(row["page_url"])
    return {
        "found": True,
        "symbol_name": str(row["qualname"] or row["name"]),
        "page_url": page_url,
        "anchor": anchor,
        "url_with_anchor": f"{page_url}#{anchor}" if anchor else page_url,
        "kind": row["kind"],
        "name": row["name"],
        "qualname": row["qualname"],
        "line_start": line_start,
        "line_end": line_end,
        "context_start": context_start,
        "context_end": context_end,
        "context": context,
        "summary": row["summary"],
        "entity_id": row["entity_id"],
    }
