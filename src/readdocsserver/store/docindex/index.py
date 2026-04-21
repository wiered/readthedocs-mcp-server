"""SQLite DocIndex: thread-safe FTS5 + pages."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ..fts_query import _fts_match_stages
from ..jsonutil import _json_list
from ..paths import default_db_path
from ..symbol_lookup import _empty_symbol_lookup, _symbol_lookup_from_row
from ..types import EntityHit, SearchHit
from .operations import (
    child_methods,
    entity_notes,
    entity_params,
    get_entity_row,
    replace_chunks,
    replace_entities,
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
                replace_entities(conn, url, source_base, entities or [])
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

    def lookup_symbol(self, source_base: str, symbol_name: str) -> dict[str, Any]:
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
                    return _symbol_lookup_from_row(row, row["page_body"])
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
                return _symbol_lookup_from_row(row, row["page_body"])
            finally:
                conn.close()

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
