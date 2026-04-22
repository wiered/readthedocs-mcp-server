"""SQLite DocIndex: thread-safe FTS5 + pages."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from readdocsserver.schemas.domain import EntityHit, EntityXrefCandidate, SearchHit
from readdocsserver.services.entity_edges import (
    build_edges_for_page,
    build_text_inference_candidates,
)
from readdocsserver.services.fts_query import _fts_match_stages
from readdocsserver.services.entity_resolver import SymbolMapsRuntime
from readdocsserver.services.symbol_lookup import (
    _empty_symbol_lookup,
    _symbol_lookup_from_row,
)
from ..paths import default_db_path
from readdocsserver.utils.jsonutil import _json_list
from .operations import (
    child_methods,
    entity_edges,
    entity_notes,
    entity_params,
    get_entity_row,
    rebuild_resolved_candidate_edges,
    replace_chunks,
    replace_edges,
    replace_entities,
    replace_xref_candidates,
    resolved_candidate_edges,
    search_chunks_fts,
    search_entities_fts,
)
from .schema import init_schema


class DocIndex:
    """Thread-safe wrapper around SQLite + FTS5."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._path = Path(db_path) if db_path else default_db_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._symbol_maps_runtime: dict[str, SymbolMapsRuntime] = {}
        self._init_schema()

    @property
    def db_path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                init_schema(conn)
            finally:
                conn.close()

    def clear_source(self, source_base: str) -> int:
        with self._lock:
            conn = self._connect()
            try:
                entity_rows = conn.execute(
                    "SELECT entity_id FROM doc_entities WHERE source_base = ?",
                    (source_base,),
                ).fetchall()
                for row in entity_rows:
                    conn.execute(
                        "DELETE FROM doc_entities_fts WHERE entity_id = ?",
                        (row["entity_id"],),
                    )
                conn.execute(
                    "DELETE FROM doc_entity_params WHERE entity_id IN "
                    "(SELECT entity_id FROM doc_entities WHERE source_base = ?)",
                    (source_base,),
                )
                conn.execute(
                    "DELETE FROM doc_entity_notes WHERE entity_id IN "
                    "(SELECT entity_id FROM doc_entities WHERE source_base = ?)",
                    (source_base,),
                )
                conn.execute(
                    "DELETE FROM doc_entity_edges WHERE source_base = ?",
                    (source_base,),
                )
                conn.execute(
                    "DELETE FROM doc_entity_xref_candidates WHERE source_base = ?",
                    (source_base,),
                )
                conn.execute(
                    "DELETE FROM doc_entities WHERE source_base = ?", (source_base,)
                )
                conn.execute(
                    "DELETE FROM search_chunks WHERE url IN (SELECT url FROM pages WHERE source_base = ?)",
                    (source_base,),
                )
                cur = conn.execute(
                    "DELETE FROM pages WHERE source_base = ?", (source_base,)
                )
                deleted = cur.rowcount or 0
                self._symbol_maps_runtime.pop(source_base, None)
                conn.commit()
                return deleted
            finally:
                conn.close()

    def upsert_page(
        self,
        url: str,
        title: str,
        body: str,
        source_base: str,
        fetched_at: int,
        entities: list[dict[str, Any]] | None = None,
        toc: list[dict[str, Any]] | None = None,
    ) -> None:
        toc_json = json.dumps(toc or [], ensure_ascii=False)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO pages(url, title, body, source_base, fetched_at, toc_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        title=excluded.title,
                        body=excluded.body,
                        source_base=excluded.source_base,
                        fetched_at=excluded.fetched_at,
                        toc_json=excluded.toc_json
                    """,
                    (url, title, body, source_base, fetched_at, toc_json),
                )
                replace_chunks(conn, url, title, body)
                runtime = self._symbol_maps_runtime.get(source_base)
                if runtime is None:
                    runtime = SymbolMapsRuntime()
                    runtime.seed_from_db(conn, source_base)
                    self._symbol_maps_runtime[source_base] = runtime
                runtime.remove_entities_for_page(conn, source_base, url)
                local_to_entity = replace_entities(
                    conn, url, source_base, entities or []
                )
                runtime.add_entity_rows(
                    self._page_entity_triplets(url, entities or [])
                )
                xref_candidates = self._xref_candidates_from_entities(
                    url, source_base, entities or [], local_to_entity
                )
                xref_candidates.extend(
                    build_text_inference_candidates(conn, source_base, url)
                )
                replace_xref_candidates(conn, url, source_base, xref_candidates)
                edges = build_edges_for_page(
                    conn, source_base, url, maps=runtime.to_symbol_maps()
                )
                edges.extend(resolved_candidate_edges(conn, source_base, page_url=url))
                replace_edges(conn, url, source_base, edges)
                conn.commit()
            finally:
                conn.close()

    def discard_symbol_maps_runtime(self, source_base: str) -> None:
        with self._lock:
            self._symbol_maps_runtime.pop(source_base, None)

    @staticmethod
    def _page_entity_triplets(
        url: str, entities: list[dict[str, Any]]
    ) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        for i, entity in enumerate(entities):
            anchor = str(entity.get("anchor") or "").strip()
            entity_id = f"{url}#{anchor}" if anchor else f"{url}#entity-{i}"
            rows.append(
                (
                    entity_id,
                    str(entity.get("name") or ""),
                    str(entity.get("qualname") or ""),
                )
            )
        return rows

    def _xref_candidates_from_entities(
        self,
        url: str,
        source_base: str,
        entities: list[dict[str, Any]],
        local_to_entity: dict[str, str],
    ) -> list[EntityXrefCandidate]:
        candidates: list[EntityXrefCandidate] = []
        for entity in entities:
            local_id = str(entity.get("local_id") or "")
            from_entity_id = local_to_entity.get(local_id)
            if from_entity_id is None:
                continue
            line_start = entity.get("line_start")
            line_end = entity.get("line_end")
            for xref in entity.get("xrefs") or []:
                candidates.append(
                    EntityXrefCandidate(
                        source_base=source_base,
                        page_url=url,
                        from_entity_id=from_entity_id,
                        edge_type=str(xref.get("edge_type") or "references"),
                        source_kind=str(xref.get("source_kind") or "xref"),
                        target_url=str(xref.get("target_url") or ""),
                        target_anchor=str(xref.get("target_anchor") or ""),
                        target_name=str(xref.get("target_name") or ""),
                        snippet=str(xref.get("snippet") or ""),
                        confidence=float(xref.get("confidence") or 1.0),
                        line_start=line_start,
                        line_end=line_end,
                    )
                )
        return candidates

    def rebuild_graph_for_source(self, source_base: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                rebuild_resolved_candidate_edges(conn, source_base)
                conn.commit()
            finally:
                conn.close()

    def search(
        self,
        query: str,
        limit: int = 15,
        *,
        source_base: str | None = None,
    ) -> list[SearchHit]:
        stages = _fts_match_stages(query)
        if not stages:
            return []
        limit = max(1, min(limit, 100))
        for fts in stages:
            hits = self._search_fts(
                fts, limit, query, source_base=source_base, dedupe_url=True
            )
            if hits:
                return hits
        return []

    def search_in_page(
        self, page_url: str, query: str, limit: int = 15
    ) -> list[SearchHit]:
        """FTS over chunks of a single page; may return several hits (chunks) for the same URL."""
        stages = _fts_match_stages(query)
        if not stages:
            return []
        limit = max(1, min(limit, 100))
        for fts in stages:
            hits = self._search_fts(
                fts,
                limit,
                query,
                page_url=page_url,
                dedupe_url=False,
            )
            if hits:
                return hits
        return []

    def _search_fts(
        self,
        fts: str,
        limit: int,
        user_query: str,
        *,
        source_base: str | None = None,
        page_url: str | None = None,
        dedupe_url: bool = True,
    ) -> list[SearchHit]:
        with self._lock:
            conn = self._connect()
            try:
                return search_chunks_fts(
                    conn,
                    fts,
                    limit,
                    user_query,
                    source_base=source_base,
                    page_url=page_url,
                    dedupe_url=dedupe_url,
                )
            finally:
                conn.close()

    def search_entities(
        self,
        query: str,
        limit: int = 15,
        *,
        kind: str | None = None,
        source_base: str | None = None,
    ) -> list[EntityHit]:
        stages = _fts_match_stages(query)
        if not stages:
            return []
        limit = max(1, min(limit, 100))
        for fts in stages:
            hits = self._search_entities_fts(
                fts, limit, kind=kind, source_base=source_base
            )
            if hits:
                return hits
        return []

    def _search_entities_fts(
        self,
        fts: str,
        limit: int,
        *,
        kind: str | None = None,
        source_base: str | None = None,
    ) -> list[EntityHit]:
        with self._lock:
            conn = self._connect()
            try:
                return search_entities_fts(
                    conn, fts, limit, kind=kind, source_base=source_base
                )
            finally:
                conn.close()

    def get_entity(
        self,
        entity_id: str,
        *,
        include_methods: bool = True,
        include_params: bool = True,
        include_notes: bool = True,
    ) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                entity = get_entity_row(conn, entity_id)
                if entity is None:
                    return None
                if include_params:
                    entity["params"] = entity_params(conn, entity_id)
                if include_notes:
                    entity["notes"] = entity_notes(conn, entity_id)
                if include_methods and entity["kind"] == "class":
                    entity["methods"] = child_methods(conn, entity_id)
                return entity
            finally:
                conn.close()

    def list_class_methods(
        self,
        *,
        class_entity_id: str | None = None,
        class_name: str | None = None,
        source_base: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                entity_id = class_entity_id
                if entity_id is None and class_name:
                    row = conn.execute(
                        """
                        SELECT entity_id FROM doc_entities
                        WHERE kind = 'class'
                          AND (name = ? OR qualname = ?)
                          AND (? IS NULL OR source_base = ?)
                        ORDER BY LENGTH(qualname), qualname
                        LIMIT 1
                        """,
                        (class_name, class_name, source_base, source_base),
                    ).fetchone()
                    entity_id = row["entity_id"] if row else None
                if entity_id is None:
                    return []
                return child_methods(conn, entity_id)
            finally:
                conn.close()

    def get_entity_context(
        self,
        query: str,
        *,
        kind: str | None = None,
        include_notes: bool = True,
        source_base: str | None = None,
    ) -> dict[str, Any] | None:
        hits = self.search_entities(query, limit=1, kind=kind, source_base=source_base)
        if not hits:
            return None
        return self.get_entity(
            hits[0].entity_id,
            include_methods=False,
            include_params=True,
            include_notes=include_notes,
        )

    def lookup_symbol(
        self, source_base: str, symbol_name: str, *, include_related: bool = False
    ) -> dict[str, Any]:
        symbol = symbol_name.strip()
        if not symbol:
            return _empty_symbol_lookup(symbol)

        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT e.entity_id, e.source_base, e.page_url, e.anchor,
                           e.kind, e.name, e.qualname, e.signature, e.summary,
                           e.body_text, e.parent_entity_id, e.line_start, e.line_end,
                           p.body AS page_body
                    FROM doc_entities AS e
                    LEFT JOIN pages AS p ON p.url = e.page_url
                    WHERE e.source_base = ?
                      AND (e.qualname = ? OR e.anchor = ? OR e.name = ?)
                    ORDER BY
                        CASE
                            WHEN e.qualname = ? THEN 0
                            WHEN e.anchor = ? THEN 1
                            ELSE 2
                        END,
                        LENGTH(e.qualname),
                        e.qualname
                    LIMIT 1
                    """,
                    (source_base, symbol, symbol, symbol, symbol, symbol),
                ).fetchone()
                if row is not None:
                    result = _symbol_lookup_from_row(row, row["page_body"])
                    if include_related:
                        result["related"] = self._compact_related(
                            conn, str(row["entity_id"])
                        )
                    return result
            finally:
                conn.close()

        hits = self.search_entities(symbol, limit=1, source_base=source_base)
        if not hits:
            return _empty_symbol_lookup(symbol)

        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT e.entity_id, e.source_base, e.page_url, e.anchor,
                           e.kind, e.name, e.qualname, e.signature, e.summary,
                           e.body_text, e.parent_entity_id, e.line_start, e.line_end,
                           p.body AS page_body
                    FROM doc_entities AS e
                    LEFT JOIN pages AS p ON p.url = e.page_url
                    WHERE e.entity_id = ?
                    """,
                    (hits[0].entity_id,),
                ).fetchone()
                if row is None:
                    return _empty_symbol_lookup(symbol)
                result = _symbol_lookup_from_row(row, row["page_body"])
                if include_related:
                    result["related"] = self._compact_related(
                        conn, str(row["entity_id"])
                    )
                return result
            finally:
                conn.close()

    def related_symbols(
        self,
        source_base: str,
        symbol_name: str,
        *,
        edge_types: list[str] | None = None,
        direction: str = "both",
        limit: int = 50,
    ) -> dict[str, Any]:
        if direction not in {"out", "in", "both"}:
            raise ValueError("direction must be 'out', 'in', or 'both'")
        symbol = symbol_name.strip()
        if not symbol:
            return {"found": False, "symbol": None, "edges": []}
        limit = max(1, min(limit, 100))

        with self._lock:
            conn = self._connect()
            try:
                symbol_row = self._find_symbol_row(conn, source_base, symbol)
                if symbol_row is None:
                    return {"found": False, "symbol": None, "edges": []}
                symbol_ref = _symbol_ref_from_row(symbol_row)
                edge_rows = self._related_edge_rows(
                    conn,
                    str(symbol_row["entity_id"]),
                    edge_types=edge_types,
                    direction=direction,
                    limit=limit,
                )
                refs = self._entity_refs_by_id(
                    conn,
                    [
                        str(edge.to_entity_id)
                        if edge.direction == "out"
                        else str(edge.from_entity_id)
                        for edge in edge_rows
                    ],
                )
                edges = []
                for edge in _sort_related_edges(edge_rows):
                    related_id = (
                        str(edge.to_entity_id)
                        if edge.direction == "out"
                        else str(edge.from_entity_id)
                    )
                    target = refs.get(related_id)
                    if target is None:
                        continue
                    edges.append(
                        {
                            "direction": edge.direction,
                            "edge_type": edge.edge_type,
                            "source_kind": edge.source_kind,
                            "param_name": edge.param_name,
                            "confidence": edge.confidence,
                            "snippet": edge.snippet,
                            "source_page_url": edge.page_url,
                            "line_start": edge.line_start,
                            "line_end": edge.line_end,
                            "relation_label": _relation_label(
                                edge.edge_type, edge.direction
                            ),
                            "target": target,
                        }
                    )
                return {"found": True, "symbol": symbol_ref, "edges": edges}
            finally:
                conn.close()

    def symbol_graph_stats(self, source_base: str) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                edge_type_counts = {
                    str(row["edge_type"]): int(row["n"])
                    for row in conn.execute(
                        """
                        SELECT edge_type, COUNT(*) AS n
                        FROM doc_entity_edges
                        WHERE source_base = ?
                        GROUP BY edge_type
                        ORDER BY edge_type
                        """,
                        (source_base,),
                    ).fetchall()
                }
                source_kind_counts = {
                    str(row["source_kind"]): int(row["n"])
                    for row in conn.execute(
                        """
                        SELECT source_kind, COUNT(*) AS n
                        FROM doc_entity_edges
                        WHERE source_base = ?
                        GROUP BY source_kind
                        ORDER BY source_kind
                        """,
                        (source_base,),
                    ).fetchall()
                }
                unresolved = conn.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM doc_entity_xref_candidates AS c
                    WHERE c.source_base = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM doc_entities AS target
                          WHERE target.source_base = c.source_base
                            AND (
                                (c.target_url = '' AND c.target_anchor != ''
                                    AND target.anchor = c.target_anchor)
                                OR (c.target_url != '' AND c.target_anchor != ''
                                    AND target.page_url = c.target_url
                                    AND target.anchor = c.target_anchor)
                                OR (c.target_name != ''
                                    AND target.qualname = c.target_name)
                                OR (c.target_name != ''
                                    AND target.name = c.target_name
                                    AND 1 = (
                                        SELECT COUNT(*) FROM doc_entities AS unique_name
                                        WHERE unique_name.source_base = c.source_base
                                          AND unique_name.name = c.target_name
                                    ))
                            )
                      )
                    """,
                    (source_base,),
                ).fetchone()
                top_unresolved = [
                    {
                        "target": row["target"],
                        "count": int(row["n"]),
                    }
                    for row in conn.execute(
                        """
                        SELECT COALESCE(NULLIF(target_anchor, ''), target_name, target_url)
                               AS target,
                               COUNT(*) AS n
                        FROM doc_entity_xref_candidates AS c
                        WHERE c.source_base = ?
                          AND NOT EXISTS (
                              SELECT 1 FROM doc_entities AS target
                              WHERE target.source_base = c.source_base
                                AND (
                                    (c.target_url = '' AND c.target_anchor != ''
                                        AND target.anchor = c.target_anchor)
                                    OR (c.target_url != '' AND c.target_anchor != ''
                                        AND target.page_url = c.target_url
                                        AND target.anchor = c.target_anchor)
                                    OR (c.target_name != ''
                                        AND target.qualname = c.target_name)
                                    OR (c.target_name != ''
                                        AND target.name = c.target_name
                                        AND 1 = (
                                            SELECT COUNT(*)
                                            FROM doc_entities AS unique_name
                                            WHERE unique_name.source_base = c.source_base
                                              AND unique_name.name = c.target_name
                                        ))
                                )
                          )
                        GROUP BY target
                        ORDER BY n DESC, target
                        LIMIT 20
                        """,
                        (source_base,),
                    ).fetchall()
                ]
                stale = conn.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM doc_entity_edges AS edge
                    LEFT JOIN doc_entities AS source
                      ON source.entity_id = edge.from_entity_id
                    LEFT JOIN doc_entities AS target
                      ON target.entity_id = edge.to_entity_id
                    WHERE edge.source_base = ?
                      AND (source.entity_id IS NULL OR target.entity_id IS NULL)
                    """,
                    (source_base,),
                ).fetchone()
                entities = conn.execute(
                    "SELECT COUNT(*) AS n FROM doc_entities WHERE source_base = ?",
                    (source_base,),
                ).fetchone()
                pages = conn.execute(
                    "SELECT COUNT(*) AS n FROM pages WHERE source_base = ?",
                    (source_base,),
                ).fetchone()
                return {
                    "source_base": source_base,
                    "edge_type_counts": edge_type_counts,
                    "source_kind_counts": source_kind_counts,
                    "unresolved_xref_target_count": int(unresolved["n"]),
                    "top_unresolved_targets": top_unresolved,
                    "stale_edge_count": int(stale["n"]),
                    "total_entities": int(entities["n"]),
                    "total_pages": int(pages["n"]),
                }
            finally:
                conn.close()

    def _find_symbol_row(
        self, conn: sqlite3.Connection, source_base: str, symbol: str
    ) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT entity_id, kind, name, qualname, page_url, anchor
            FROM doc_entities
            WHERE source_base = ?
              AND (qualname = ? OR anchor = ? OR name = ?)
            ORDER BY
                CASE
                    WHEN qualname = ? THEN 0
                    WHEN anchor = ? THEN 1
                    ELSE 2
                END,
                LENGTH(qualname),
                qualname
            LIMIT 1
            """,
            (source_base, symbol, symbol, symbol, symbol, symbol),
        ).fetchone()
        if row is not None:
            return row

        hits = []
        for fts in _fts_match_stages(symbol):
            hits = search_entities_fts(conn, fts, 1, source_base=source_base)
            if hits:
                break
        if not hits:
            return None
        return conn.execute(
            """
            SELECT entity_id, kind, name, qualname, page_url, anchor
            FROM doc_entities
            WHERE entity_id = ?
            """,
            (hits[0].entity_id,),
        ).fetchone()

    def _related_edge_rows(
        self,
        conn: sqlite3.Connection,
        entity_id: str,
        *,
        edge_types: list[str] | None,
        direction: str,
        limit: int,
    ) -> list[_DirectedEdge]:
        directions = ["out", "in"] if direction == "both" else [direction]
        requested_types = edge_types or [None]
        rows: list[_DirectedEdge] = []
        for edge_direction in directions:
            for edge_type in requested_types:
                for edge in entity_edges(
                    conn,
                    entity_id,
                    direction=edge_direction,
                    edge_type=edge_type,
                    limit=limit,
                ):
                    rows.append(_DirectedEdge(edge_direction, edge))
                    if len(rows) >= limit:
                        return rows
        return rows

    def _compact_related(
        self, conn: sqlite3.Connection, entity_id: str, limit: int = 30
    ) -> dict[str, list[dict[str, Any]]]:
        edges = self._related_edge_rows(
            conn, entity_id, edge_types=None, direction="both", limit=limit
        )
        refs = self._entity_refs_by_id(
            conn,
            [
                str(edge.to_entity_id)
                if edge.direction == "out"
                else str(edge.from_entity_id)
                for edge in edges
            ],
        )
        out: dict[str, list[dict[str, Any]]] = {"out": [], "in": []}
        for edge in _sort_related_edges(edges):
            related_id = (
                str(edge.to_entity_id)
                if edge.direction == "out"
                else str(edge.from_entity_id)
            )
            target = refs.get(related_id)
            if target is None:
                continue
            out[edge.direction].append(
                {
                    "edge_type": edge.edge_type,
                    "relation_label": _relation_label(edge.edge_type, edge.direction),
                    "source_kind": edge.source_kind,
                    "confidence": edge.confidence,
                    "param_name": edge.param_name,
                    "snippet": edge.snippet,
                    "source_page_url": edge.page_url,
                    "line_start": edge.line_start,
                    "line_end": edge.line_end,
                    "target": target,
                }
            )
        return out

    def _entity_refs_by_id(
        self, conn: sqlite3.Connection, entity_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        if not entity_ids:
            return {}
        refs: dict[str, dict[str, Any]] = {}
        for entity_id in sorted(set(entity_ids)):
            row = conn.execute(
                """
                SELECT entity_id, kind, qualname, page_url, anchor
                FROM doc_entities
                WHERE entity_id = ?
                """,
                (entity_id,),
            ).fetchone()
            if row is not None:
                refs[str(row["entity_id"])] = _symbol_ref_from_row(row)
        return refs

    def get_page(self, url: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT url, title, body, source_base, fetched_at FROM pages WHERE url = ?",
                    (url,),
                ).fetchone()
                if row is None:
                    return None
                return {
                    "id": row["url"],
                    "title": row["title"],
                    "text": row["body"],
                    "url": row["url"],
                    "metadata": {
                        "source_base": row["source_base"],
                        "fetched_at": row["fetched_at"],
                    },
                }
            finally:
                conn.close()

    def list_sources(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT source_base, COUNT(*) AS n,
                           MAX(fetched_at) AS last_fetch
                    FROM pages
                    GROUP BY source_base
                    ORDER BY source_base
                    """
                ).fetchall()
                return [
                    {
                        "source_base": r["source_base"],
                        "page_count": r["n"],
                        "last_fetched_at": r["last_fetch"],
                    }
                    for r in rows
                ]
            finally:
                conn.close()

    def list_pages(
        self,
        *,
        source_base: str | None = None,
        url_contains: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return (page rows, total matching count) ordered by URL."""
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        where_parts: list[str] = []
        args: list[Any] = []
        if source_base is not None:
            where_parts.append("source_base = ?")
            args.append(source_base)
        if url_contains:
            where_parts.append("INSTR(url, ?) > 0")
            args.append(url_contains)
        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        with self._lock:
            conn = self._connect()
            try:
                total_row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM pages {where_sql}", args
                ).fetchone()
                total = int(total_row["n"]) if total_row else 0
                rows = conn.execute(
                    f"""
                    SELECT url, title, source_base, toc_json
                    FROM pages
                    {where_sql}
                    ORDER BY url
                    LIMIT ? OFFSET ?
                    """,
                    [*args, limit, offset],
                ).fetchall()
                return [
                    {
                        "url": r["url"],
                        "title": r["title"],
                        "source_base": r["source_base"],
                        "toc": _json_list(str(r["toc_json"] or "[]")),
                    }
                    for r in rows
                ], total
            finally:
                conn.close()

    def stats_json(self) -> str:
        return json.dumps({"sources": self.list_sources(), "db_path": str(self._path)})


class _DirectedEdge:
    def __init__(self, direction: str, edge: Any) -> None:
        self.direction = direction
        self.from_entity_id = edge.from_entity_id
        self.to_entity_id = edge.to_entity_id
        self.edge_type = edge.edge_type
        self.source_kind = edge.source_kind
        self.param_name = edge.param_name
        self.confidence = edge.confidence
        self.snippet = edge.snippet
        self.page_url = edge.page_url
        self.line_start = edge.line_start
        self.line_end = edge.line_end


def _symbol_ref_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "entity_id": row["entity_id"],
        "qualname": row["qualname"],
        "kind": row["kind"],
        "page_url": row["page_url"],
        "anchor": row["anchor"],
    }


def _relation_label(edge_type: str, direction: str) -> str:
    if direction == "in":
        return {
            "returns": "returned_by",
            "accepts_parameter_type": "used_by",
            "has_method": "method_of",
        }.get(edge_type, edge_type)
    return edge_type


def _sort_related_edges(edges: list[_DirectedEdge]) -> list[_DirectedEdge]:
    source_rank = {"structure": 0, "signature": 0, "xref": 1, "text_inference": 2}
    return sorted(
        edges,
        key=lambda edge: (
            source_rank.get(edge.source_kind, 9),
            -edge.confidence,
            len(
                str(
                    edge.to_entity_id
                    if edge.direction == "out"
                    else edge.from_entity_id
                )
            ),
            edge.edge_type,
        ),
    )
