"""Stateless SQLite helpers for DocIndex (chunks, entities, FTS search)."""

from __future__ import annotations

import sqlite3
from typing import Any

from readdocsserver.schemas.domain import (
    EntityEdge,
    EntityHit,
    EntityXrefCandidate,
    SearchHit,
)
from readdocsserver.services.chunking import _body_to_chunks_with_lines
from readdocsserver.services.entity_fts import _entity_fts_body_text
from readdocsserver.services.snippets import (
    _approx_line_in_body,
    _context_line_info_from_chunk,
)


def replace_chunks(conn: sqlite3.Connection, url: str, title: str, body: str) -> None:
    conn.execute("DELETE FROM search_chunks WHERE url = ?", (url,))
    parts = _body_to_chunks_with_lines(body)
    for i, part in enumerate(parts):
        conn.execute(
            """
            INSERT INTO search_chunks(url, ord, line_start, line_end, title, chunk)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (url, i, part.line_start, part.line_end, title, part.text),
        )


def _entity_edge_from_row(row: sqlite3.Row) -> EntityEdge:
    return EntityEdge(
        edge_id=row["edge_id"],
        source_base=row["source_base"],
        from_entity_id=row["from_entity_id"],
        to_entity_id=row["to_entity_id"],
        edge_type=row["edge_type"],
        source_kind=row["source_kind"],
        param_name=row["param_name"],
        confidence=float(row["confidence"]),
        snippet=row["snippet"],
        page_url=row["page_url"],
        line_start=row["line_start"],
        line_end=row["line_end"],
    )


def delete_edges_for_page(conn: sqlite3.Connection, url: str) -> None:
    conn.execute("DELETE FROM doc_entity_edges WHERE page_url = ?", (url,))
    conn.execute("DELETE FROM doc_entity_xref_candidates WHERE page_url = ?", (url,))
    conn.execute(
        "DELETE FROM doc_entity_edges WHERE from_entity_id IN "
        "(SELECT entity_id FROM doc_entities WHERE page_url = ?)",
        (url,),
    )
    conn.execute(
        "DELETE FROM doc_entity_xref_candidates WHERE from_entity_id IN "
        "(SELECT entity_id FROM doc_entities WHERE page_url = ?)",
        (url,),
    )


def _delete_stale_incoming_edges(
    conn: sqlite3.Connection, old_entity_ids: list[str], new_entity_ids: list[str]
) -> None:
    stale_ids = sorted(set(old_entity_ids) - set(new_entity_ids))
    for entity_id in stale_ids:
        conn.execute(
            "DELETE FROM doc_entity_edges WHERE to_entity_id = ?",
            (entity_id,),
        )


def replace_edges(
    conn: sqlite3.Connection,
    url: str,
    source_base: str,
    edges: list[EntityEdge],
) -> None:
    conn.execute("DELETE FROM doc_entity_edges WHERE page_url = ?", (url,))
    conn.execute(
        "DELETE FROM doc_entity_edges WHERE from_entity_id IN "
        "(SELECT entity_id FROM doc_entities WHERE page_url = ?)",
        (url,),
    )
    for edge in edges:
        conn.execute(
            """
            INSERT OR IGNORE INTO doc_entity_edges(
                source_base, from_entity_id, to_entity_id, edge_type, source_kind,
                param_name, confidence, snippet, page_url, line_start, line_end
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_base,
                edge.from_entity_id,
                edge.to_entity_id,
                edge.edge_type,
                edge.source_kind,
                edge.param_name,
                edge.confidence,
                edge.snippet,
                url,
                edge.line_start,
                edge.line_end,
            ),
        )


# Shared JOIN for resolving xref / text_inference candidates to doc_entities.
_XREF_RESOLVE_JOIN = """
        FROM doc_entity_xref_candidates AS c
        JOIN doc_entities AS e
          ON e.source_base = c.source_base
         AND (
             (c.target_url = '' AND c.target_anchor != ''
                 AND e.anchor = c.target_anchor)
             OR (c.target_url != '' AND c.target_anchor != ''
                 AND e.page_url = c.target_url AND e.anchor = c.target_anchor)
             OR (c.target_name != '' AND e.qualname = c.target_name)
             OR (c.target_name != '' AND e.name = c.target_name
                 AND 1 = (
                     SELECT COUNT(*) FROM doc_entities AS u
                     WHERE u.source_base = c.source_base AND u.name = c.target_name
                 ))
         )
"""


def replace_xref_candidates(
    conn: sqlite3.Connection,
    url: str,
    source_base: str,
    candidates: list[EntityXrefCandidate],
) -> None:
    conn.execute("DELETE FROM doc_entity_xref_candidates WHERE page_url = ?", (url,))
    for candidate in candidates:
        conn.execute(
            """
            INSERT OR IGNORE INTO doc_entity_xref_candidates(
                source_base, page_url, from_entity_id, edge_type, source_kind,
                target_url, target_anchor, target_name, snippet, confidence,
                line_start, line_end
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_base,
                url,
                candidate.from_entity_id,
                candidate.edge_type,
                candidate.source_kind,
                candidate.target_url,
                candidate.target_anchor,
                candidate.target_name,
                candidate.snippet,
                candidate.confidence,
                candidate.line_start,
                candidate.line_end,
            ),
        )


def resolved_candidate_edges(
    conn: sqlite3.Connection,
    source_base: str,
    *,
    page_url: str | None = None,
) -> list[EntityEdge]:
    page_filter = " AND c.page_url = ?" if page_url is not None else ""
    args: list[Any] = [source_base]
    if page_url is not None:
        args.append(page_url)
    rows = conn.execute(
        f"""
        SELECT c.source_base, c.from_entity_id, e.entity_id AS to_entity_id,
               c.edge_type, c.source_kind, c.snippet, c.page_url, c.confidence,
               c.line_start, c.line_end
        {_XREF_RESOLVE_JOIN}
        WHERE c.source_base = ?{page_filter}
        ORDER BY c.page_url, c.from_entity_id, c.edge_type, e.entity_id
        """,
        args,
    ).fetchall()
    return [
        EntityEdge(
            edge_id=None,
            source_base=row["source_base"],
            from_entity_id=row["from_entity_id"],
            to_entity_id=row["to_entity_id"],
            edge_type=row["edge_type"],
            source_kind=row["source_kind"],
            confidence=float(row["confidence"]),
            snippet=row["snippet"],
            page_url=row["page_url"],
            line_start=row["line_start"],
            line_end=row["line_end"],
        )
        for row in rows
    ]


def rebuild_resolved_candidate_edges(
    conn: sqlite3.Connection, source_base: str
) -> None:
    conn.execute(
        "DELETE FROM doc_entity_edges WHERE source_base = ? "
        "AND source_kind IN ('xref', 'text_inference')",
        (source_base,),
    )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO doc_entity_edges(
            source_base, from_entity_id, to_entity_id, edge_type, source_kind,
            param_name, confidence, snippet, page_url, line_start, line_end
        )
        SELECT c.source_base, c.from_entity_id, e.entity_id,
               c.edge_type, c.source_kind, '', c.confidence, c.snippet, c.page_url,
               c.line_start, c.line_end
        {_XREF_RESOLVE_JOIN}
        WHERE c.source_base = ?
        """,
        (source_base,),
    )


def entity_edges(
    conn: sqlite3.Connection,
    entity_id: str,
    direction: str = "out",
    edge_type: str | None = None,
    limit: int = 50,
) -> list[EntityEdge]:
    if direction not in {"out", "in"}:
        raise ValueError("direction must be 'out' or 'in'")
    limit = max(1, min(limit, 500))
    column = "from_entity_id" if direction == "out" else "to_entity_id"
    edge_filter = " AND edge_type = ?" if edge_type is not None else ""
    args: list[Any] = [entity_id]
    if edge_type is not None:
        args.append(edge_type)
    args.append(limit)
    rows = conn.execute(
        f"""
        SELECT edge_id, source_base, from_entity_id, to_entity_id, edge_type,
               source_kind, param_name, confidence, snippet, page_url, line_start,
               line_end
        FROM doc_entity_edges
        WHERE {column} = ?{edge_filter}
        ORDER BY edge_type, source_kind, to_entity_id, from_entity_id, edge_id
        LIMIT ?
        """,
        args,
    ).fetchall()
    return [_entity_edge_from_row(row) for row in rows]


def entity_edges_between(
    conn: sqlite3.Connection, from_entity_id: str, to_entity_id: str
) -> list[EntityEdge]:
    rows = conn.execute(
        """
        SELECT edge_id, source_base, from_entity_id, to_entity_id, edge_type,
               source_kind, param_name, confidence, snippet, page_url, line_start,
               line_end
        FROM doc_entity_edges
        WHERE from_entity_id = ? AND to_entity_id = ?
        ORDER BY edge_type, source_kind, param_name, edge_id
        """,
        (from_entity_id, to_entity_id),
    ).fetchall()
    return [_entity_edge_from_row(row) for row in rows]


def delete_entities_for_page(conn: sqlite3.Connection, url: str) -> None:
    delete_edges_for_page(conn, url)
    entity_rows = conn.execute(
        "SELECT entity_id FROM doc_entities WHERE page_url = ?", (url,)
    ).fetchall()
    entity_ids = [row["entity_id"] for row in entity_rows]
    for entity_id in entity_ids:
        conn.execute("DELETE FROM doc_entities_fts WHERE entity_id = ?", (entity_id,))
    conn.execute(
        "DELETE FROM doc_entity_params WHERE entity_id IN "
        "(SELECT entity_id FROM doc_entities WHERE page_url = ?)",
        (url,),
    )
    conn.execute(
        "DELETE FROM doc_entity_notes WHERE entity_id IN "
        "(SELECT entity_id FROM doc_entities WHERE page_url = ?)",
        (url,),
    )
    conn.execute("DELETE FROM doc_entities WHERE page_url = ?", (url,))


def rebuild_doc_entities_fts(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM doc_entities_fts")
    rows = conn.execute(
        """
        SELECT entity_id, name, qualname, signature, summary, body_text
        FROM doc_entities
        ORDER BY entity_id
        """
    ).fetchall()
    for row in rows:
        notes = conn.execute(
            """
            SELECT kind, version, text
            FROM doc_entity_notes
            WHERE entity_id = ?
            ORDER BY ord
            """,
            (row["entity_id"],),
        ).fetchall()
        conn.execute(
            """
            INSERT INTO doc_entities_fts(
                entity_id, name, qualname, signature, summary, body_text
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row["entity_id"],
                row["name"],
                row["qualname"],
                row["signature"],
                row["summary"],
                _entity_fts_body_text(str(row["body_text"] or ""), notes),
            ),
        )


def replace_entities(
    conn: sqlite3.Connection,
    url: str,
    source_base: str,
    entities: list[dict[str, Any]],
) -> dict[str, str]:
    old_rows = conn.execute(
        "SELECT entity_id FROM doc_entities WHERE page_url = ?", (url,)
    ).fetchall()
    old_entity_ids = [row["entity_id"] for row in old_rows]
    local_to_entity: dict[str, str] = {}
    entity_ids: list[str] = []
    for i, entity in enumerate(entities):
        local_id = str(entity.get("local_id") or "")
        anchor = str(entity.get("anchor") or "").strip()
        entity_id = f"{url}#{anchor}" if anchor else f"{url}#entity-{i}"
        if local_id:
            local_to_entity[local_id] = entity_id
        entity_ids.append(entity_id)

    delete_entities_for_page(conn, url)
    _delete_stale_incoming_edges(conn, old_entity_ids, entity_ids)

    for i, entity in enumerate(entities):
        entity_id = entity_ids[i]
        parent_local_id = entity.get("parent_local_id")
        parent_entity_id = (
            local_to_entity.get(str(parent_local_id)) if parent_local_id else None
        )
        conn.execute(
            """
            INSERT INTO doc_entities(
                entity_id, source_base, page_url, anchor, kind, name, qualname,
                signature, summary, body_text, parent_entity_id, line_start, line_end
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_id,
                source_base,
                url,
                entity.get("anchor") or None,
                str(entity.get("kind") or ""),
                str(entity.get("name") or ""),
                str(entity.get("qualname") or ""),
                str(entity.get("signature") or ""),
                str(entity.get("summary") or ""),
                str(entity.get("body_text") or ""),
                parent_entity_id,
                entity.get("line_start"),
                entity.get("line_end"),
            ),
        )
        conn.execute(
            """
            INSERT INTO doc_entities_fts(
                entity_id, name, qualname, signature, summary, body_text
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                entity_id,
                str(entity.get("name") or ""),
                str(entity.get("qualname") or ""),
                str(entity.get("signature") or ""),
                str(entity.get("summary") or ""),
                _entity_fts_body_text(
                    str(entity.get("body_text") or ""), entity.get("notes") or []
                ),
            ),
        )
        for param in entity.get("params") or []:
            conn.execute(
                """
                INSERT INTO doc_entity_params(
                    entity_id, ord, name, type, default_value, description
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    entity_id,
                    int(param.get("ord") or 0),
                    str(param.get("name") or ""),
                    str(param.get("type") or ""),
                    str(param.get("default") or ""),
                    str(param.get("description") or ""),
                ),
            )
        for note in entity.get("notes") or []:
            conn.execute(
                """
                INSERT INTO doc_entity_notes(entity_id, ord, kind, version, text)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    entity_id,
                    int(note.get("ord") or 0),
                    str(note.get("kind") or ""),
                    str(note.get("version") or ""),
                    str(note.get("text") or ""),
                ),
            )
    return local_to_entity


def search_chunks_fts(
    conn: sqlite3.Connection,
    fts: str,
    limit: int,
    user_query: str,
    *,
    source_base: str | None = None,
    page_url: str | None = None,
    dedupe_url: bool = True,
) -> list[SearchHit]:
    fetch_cap = min(1000, max(200, limit * 40))
    if not dedupe_url:
        fetch_cap = min(2000, max(fetch_cap, limit * 80))
    where_extra = ""
    extra_args: list[Any] = []
    if source_base is not None:
        where_extra += " AND p.source_base = ?"
        extra_args.append(source_base)
    if page_url is not None:
        where_extra += " AND p.url = ?"
        extra_args.append(page_url)
    sql = f"""
        SELECT p.url AS url,
               p.title AS title,
               sc.chunk AS chunk,
               sc.line_start AS line_start,
               sc.line_end AS line_end,
               bm25(search_chunks_fts) AS rnk
        FROM search_chunks_fts
        JOIN search_chunks AS sc ON search_chunks_fts.rowid = sc.chunk_id
        JOIN pages AS p ON p.url = sc.url
        WHERE search_chunks_fts MATCH ?{where_extra}
        ORDER BY bm25(search_chunks_fts) ASC, p.url ASC, sc.ord ASC
        LIMIT ?
    """
    try:
        rows = conn.execute(sql, (fts, *extra_args, fetch_cap)).fetchall()
        seen: set[str] = set()
        out: list[SearchHit] = []
        body_cache: dict[str, str] = {}
        for row in rows:
            url = row["url"]
            if dedupe_url:
                if url in seen:
                    continue
                seen.add(url)
            snippet, rel_line = _context_line_info_from_chunk(
                row["chunk"] or "", user_query
            )
            line_start = int(row["line_start"] or 1)
            line_end = int(row["line_end"] or line_start)
            if "\n" in (row["chunk"] or "") and rel_line is not None:
                hit_line = line_start + rel_line
            else:
                body = body_cache.get(url)
                if body is None:
                    body_row = conn.execute(
                        "SELECT body FROM pages WHERE url = ?", (url,)
                    ).fetchone()
                    body = str(body_row["body"]) if body_row else ""
                    body_cache[url] = body
                hit_line = _approx_line_in_body(
                    body, line_start, line_end, user_query, snippet
                )
            out.append(
                SearchHit(
                    url=url,
                    title=row["title"],
                    snippet=snippet,
                    rank=float(row["rnk"]),
                    line=hit_line,
                    chunk_line_start=line_start,
                    chunk_line_end=line_end,
                )
            )
            if len(out) >= limit:
                break
        return out
    except sqlite3.OperationalError:
        return []


def search_entities_fts(
    conn: sqlite3.Connection,
    fts: str,
    limit: int,
    *,
    kind: str | None = None,
    source_base: str | None = None,
) -> list[EntityHit]:
    where_extra = ""
    extra_args: list[Any] = []
    if kind is not None:
        where_extra += " AND e.kind = ?"
        extra_args.append(kind)
    if source_base is not None:
        where_extra += " AND e.source_base = ?"
        extra_args.append(source_base)
    scoped_fts = f"{{name qualname signature summary}} : ({fts})"
    sql = f"""
        SELECT e.entity_id, e.kind, e.name, e.qualname, e.signature, e.summary,
               e.page_url, e.anchor, e.parent_entity_id, e.line_start, e.line_end,
               bm25(doc_entities_fts) AS rnk
        FROM doc_entities_fts
        JOIN doc_entities AS e ON e.entity_id = doc_entities_fts.entity_id
        WHERE doc_entities_fts MATCH ?{where_extra}
        ORDER BY bm25(doc_entities_fts) ASC, e.qualname ASC
        LIMIT ?
    """
    try:
        rows = conn.execute(sql, (scoped_fts, *extra_args, limit)).fetchall()
        return [
            EntityHit(
                entity_id=row["entity_id"],
                kind=row["kind"],
                name=row["name"],
                qualname=row["qualname"],
                signature=row["signature"],
                summary=row["summary"],
                page_url=row["page_url"],
                anchor=row["anchor"],
                rank=float(row["rnk"]),
                line_start=row["line_start"],
                line_end=row["line_end"],
                parent_entity_id=row["parent_entity_id"],
            )
            for row in rows
        ]
    except sqlite3.OperationalError:
        return []


def get_entity_row(conn: sqlite3.Connection, entity_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT entity_id, source_base, page_url, anchor, kind, name, qualname,
               signature, summary, body_text, parent_entity_id, line_start, line_end
        FROM doc_entities
        WHERE entity_id = ?
        """,
        (entity_id,),
    ).fetchone()
    return dict(row) if row else None


def entity_params(conn: sqlite3.Connection, entity_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ord, name, type, default_value, description
        FROM doc_entity_params
        WHERE entity_id = ?
        ORDER BY ord
        """,
        (entity_id,),
    ).fetchall()
    return [
        {
            "ord": row["ord"],
            "name": row["name"],
            "type": row["type"],
            "default": row["default_value"],
            "description": row["description"],
        }
        for row in rows
    ]


def entity_notes(conn: sqlite3.Connection, entity_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ord, kind, version, text
        FROM doc_entity_notes
        WHERE entity_id = ?
        ORDER BY ord
        """,
        (entity_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def child_methods(
    conn: sqlite3.Connection, class_entity_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT entity_id, source_base, page_url, anchor, kind, name, qualname,
               signature, summary, body_text, parent_entity_id, line_start, line_end
        FROM doc_entities
        WHERE parent_entity_id = ? AND kind = 'method'
        ORDER BY name, signature
        """,
        (class_entity_id,),
    ).fetchall()
    methods: list[dict[str, Any]] = []
    for row in rows:
        method = dict(row)
        method["params"] = entity_params(conn, row["entity_id"])
        method["notes"] = entity_notes(conn, row["entity_id"])
        methods.append(method)
    return methods
