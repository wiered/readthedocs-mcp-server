"""Build typed symbol graph edges from indexed documentation entities."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .entity_resolver import (
    build_symbol_maps,
    resolve_symbol_name,
    resolve_type_names,
)
from .types import EntityEdge, EntityXrefCandidate

_RETURN_RE = re.compile(r"(?:->|→)\s*(.+)$")
_CLASS_BASE_RE = re.compile(
    r"^(?:class\s+)?[A-Za-z_][A-Za-z0-9_.]*\s*\((?P<bases>[^)]*)\)"
)
_INFERENCE_PATTERNS: tuple[tuple[str, re.Pattern[str], float], ...] = (
    (
        "only_valid_in",
        re.compile(r"\bcan only be used in (?P<name>[A-Z][A-Za-z0-9_.]*)\b", re.I),
        0.7,
    ),
    (
        "requires",
        re.compile(r"\bmust be used with (?P<name>[A-Z][A-Za-z0-9_.]*)\b", re.I),
        0.7,
    ),
    (
        "use_instead",
        re.compile(r"\buse (?P<name>[A-Z][A-Za-z0-9_.]*) instead\b", re.I),
        0.75,
    ),
    (
        "similar_to",
        re.compile(r"\bsimilar to (?P<name>[A-Z][A-Za-z0-9_.]*)\b", re.I),
        0.65,
    ),
    (
        "converts_to",
        re.compile(r"\bconverted to (?P<name>[A-Z][A-Za-z0-9_.]*)\b", re.I),
        0.7,
    ),
)


def build_edges_for_page(
    conn: sqlite3.Connection,
    source_base: str,
    page_url: str,
) -> list[EntityEdge]:
    """Build typed edges for entities stored on one page."""
    maps = build_symbol_maps(conn, source_base)
    entities = conn.execute(
        """
        SELECT entity_id, kind, qualname, signature, parent_entity_id,
               line_start, line_end
        FROM doc_entities
        WHERE source_base = ? AND page_url = ?
        ORDER BY entity_id
        """,
        (source_base, page_url),
    ).fetchall()

    edges: list[EntityEdge] = []
    for entity in entities:
        entity_id = str(entity["entity_id"])
        kind = str(entity["kind"] or "")
        qualname = str(entity["qualname"] or "")
        signature = str(entity["signature"] or "")
        line_start = entity["line_start"]
        line_end = entity["line_end"]

        if kind == "method" and entity["parent_entity_id"]:
            edges.append(
                _edge(
                    source_base,
                    entity["parent_entity_id"],
                    entity_id,
                    "has_method",
                    "structure",
                    page_url,
                    line_start=line_start,
                    line_end=line_end,
                )
            )

        for type_name in _return_type_names(signature):
            target_id = resolve_symbol_name(maps, qualname, type_name)
            if target_id is not None:
                edges.append(
                    _edge(
                        source_base,
                        entity_id,
                        target_id,
                        "returns",
                        "signature",
                        page_url,
                        snippet=_return_annotation(signature),
                        line_start=line_start,
                        line_end=line_end,
                    )
                )

        if kind == "class":
            for base_name in _base_type_names(signature):
                target_id = resolve_symbol_name(maps, qualname, base_name)
                if target_id is not None:
                    edges.append(
                        _edge(
                            source_base,
                            entity_id,
                            target_id,
                            "inherits_from",
                            "signature",
                            page_url,
                            snippet=_base_annotation(signature),
                            line_start=line_start,
                            line_end=line_end,
                        )
                    )

        for param in _params_for_entity(conn, entity_id):
            param_name = str(param["name"] or "")
            param_type = str(param["type"] or "")
            for type_name in resolve_type_names(param_type):
                target_id = resolve_symbol_name(maps, qualname, type_name)
                if target_id is not None:
                    edges.append(
                        _edge(
                            source_base,
                            entity_id,
                            target_id,
                            "accepts_parameter_type",
                            "signature",
                            page_url,
                            param_name=param_name,
                            snippet=param_type,
                            line_start=line_start,
                            line_end=line_end,
                        )
                    )
    return edges


def build_text_inference_candidates(
    conn: sqlite3.Connection,
    source_base: str,
    page_url: str,
) -> list[EntityXrefCandidate]:
    rows = conn.execute(
        """
        SELECT entity_id, body_text, line_start, line_end
        FROM doc_entities
        WHERE source_base = ? AND page_url = ?
        ORDER BY entity_id
        """,
        (source_base, page_url),
    ).fetchall()
    candidates: list[EntityXrefCandidate] = []
    for row in rows:
        texts = [str(row["body_text"] or "")]
        note_rows = conn.execute(
            "SELECT text FROM doc_entity_notes WHERE entity_id = ? ORDER BY ord",
            (row["entity_id"],),
        ).fetchall()
        texts.extend(str(note["text"] or "") for note in note_rows)
        for text in texts:
            candidates.extend(
                _text_inference_candidates_for_text(
                    source_base,
                    page_url,
                    str(row["entity_id"]),
                    text,
                    row["line_start"],
                    row["line_end"],
                )
            )
    return candidates


def _text_inference_candidates_for_text(
    source_base: str,
    page_url: str,
    entity_id: str,
    text: str,
    line_start: int | None,
    line_end: int | None,
) -> list[EntityXrefCandidate]:
    candidates: list[EntityXrefCandidate] = []
    for edge_type, pattern, confidence in _INFERENCE_PATTERNS:
        for match in pattern.finditer(text):
            target_name = match.group("name").strip(".,;:)")
            candidates.append(
                EntityXrefCandidate(
                    source_base=source_base,
                    page_url=page_url,
                    from_entity_id=entity_id,
                    edge_type=edge_type,
                    source_kind="text_inference",
                    target_name=target_name,
                    snippet=_sentence_snippet(text, match.start(), match.end()),
                    confidence=confidence,
                    line_start=line_start,
                    line_end=line_end,
                )
            )
    for match in re.finditer(r"\bfrom_(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b", text):
        raw = match.group("name").strip("_")
        if raw:
            candidates.append(
                EntityXrefCandidate(
                    source_base=source_base,
                    page_url=page_url,
                    from_entity_id=entity_id,
                    edge_type="converts_to",
                    source_kind="text_inference",
                    target_name=_camel_name(raw),
                    snippet=_sentence_snippet(text, match.start(), match.end()),
                    confidence=0.6,
                    line_start=line_start,
                    line_end=line_end,
                )
            )
    return candidates


def _sentence_snippet(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start))
    right_candidates = [
        pos for pos in (text.find(".", end), text.find("\n", end)) if pos != -1
    ]
    right = min(right_candidates) if right_candidates else min(len(text), end + 120)
    return text[left + 1 : right + 1].strip()[:300]


def _camel_name(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_") if part)


def _params_for_entity(conn: sqlite3.Connection, entity_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT name, type
        FROM doc_entity_params
        WHERE entity_id = ?
        ORDER BY ord
        """,
        (entity_id,),
    ).fetchall()


def _return_annotation(signature: str) -> str:
    match = _RETURN_RE.search(signature)
    return match.group(1).strip() if match else ""


def _return_type_names(signature: str) -> list[str]:
    annotation = _return_annotation(signature)
    return resolve_type_names(annotation) if annotation else []


def _base_annotation(signature: str) -> str:
    match = _CLASS_BASE_RE.match(signature.strip())
    return match.group("bases").strip() if match else ""


def _base_type_names(signature: str) -> list[str]:
    annotation = _base_annotation(signature)
    return resolve_type_names(annotation) if annotation else []


def _edge(
    source_base: str,
    from_entity_id: Any,
    to_entity_id: Any,
    edge_type: str,
    source_kind: str,
    page_url: str,
    *,
    param_name: str = "",
    snippet: str = "",
    line_start: int | None = None,
    line_end: int | None = None,
) -> EntityEdge:
    return EntityEdge(
        edge_id=None,
        source_base=source_base,
        from_entity_id=str(from_entity_id),
        to_entity_id=str(to_entity_id),
        edge_type=edge_type,
        source_kind=source_kind,
        param_name=param_name,
        confidence=1.0,
        snippet=snippet,
        page_url=page_url,
        line_start=line_start,
        line_end=line_end,
    )
