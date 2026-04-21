"""SQLite DocIndex: thread-safe FTS5 + pages."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .chunking import _body_to_chunks_with_lines
from .entity_fts import _entity_fts_body_text
from .fts_query import _fts_match_stages
from .jsonutil import _json_list
from .paths import default_db_path
from .snippets import _approx_line_in_body, _context_line_info_from_chunk
from .symbol_lookup import _empty_symbol_lookup, _symbol_lookup_from_row
from .types import EntityHit, SearchHit


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
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS pages (
                        url TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        body TEXT NOT NULL,
                        source_base TEXT NOT NULL,
                        fetched_at INTEGER NOT NULL,
                        toc_json TEXT NOT NULL DEFAULT '[]'
                    );
                    CREATE INDEX IF NOT EXISTS idx_pages_source ON pages(source_base);

                    CREATE TABLE IF NOT EXISTS search_chunks (
                        chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT NOT NULL,
                        ord INTEGER NOT NULL,
                        line_start INTEGER NOT NULL,
                        line_end INTEGER NOT NULL,
                        title TEXT NOT NULL,
                        chunk TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_search_chunks_url ON search_chunks(url);

                    CREATE VIRTUAL TABLE IF NOT EXISTS search_chunks_fts USING fts5(
                        url UNINDEXED,
                        title,
                        chunk,
                        content='search_chunks',
                        content_rowid='chunk_id',
                        tokenize='porter unicode61'
                    );
                    CREATE TRIGGER IF NOT EXISTS search_chunks_ai AFTER INSERT ON search_chunks BEGIN
                        INSERT INTO search_chunks_fts(rowid, url, title, chunk)
                        VALUES (new.chunk_id, new.url, new.title, new.chunk);
                    END;
                    CREATE TRIGGER IF NOT EXISTS search_chunks_ad AFTER DELETE ON search_chunks BEGIN
                        INSERT INTO search_chunks_fts(search_chunks_fts, rowid, url, title, chunk)
                        VALUES ('delete', old.chunk_id, old.url, old.title, old.chunk);
                    END;
                    CREATE TRIGGER IF NOT EXISTS search_chunks_au AFTER UPDATE ON search_chunks BEGIN
                        INSERT INTO search_chunks_fts(search_chunks_fts, rowid, url, title, chunk)
                        VALUES ('delete', old.chunk_id, old.url, old.title, old.chunk);
                        INSERT INTO search_chunks_fts(rowid, url, title, chunk)
                        VALUES (new.chunk_id, new.url, new.title, new.chunk);
                    END;

                    CREATE TABLE IF NOT EXISTS doc_entities (
                        entity_id TEXT PRIMARY KEY,
                        source_base TEXT NOT NULL,
                        page_url TEXT NOT NULL,
                        anchor TEXT,
                        kind TEXT NOT NULL,
                        name TEXT NOT NULL,
                        qualname TEXT NOT NULL,
                        signature TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        body_text TEXT NOT NULL,
                        parent_entity_id TEXT,
                        line_start INTEGER,
                        line_end INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS idx_doc_entities_page ON doc_entities(page_url);
                    CREATE INDEX IF NOT EXISTS idx_doc_entities_source ON doc_entities(source_base);
                    CREATE INDEX IF NOT EXISTS idx_doc_entities_kind_name ON doc_entities(kind, name);
                    CREATE INDEX IF NOT EXISTS idx_doc_entities_parent ON doc_entities(parent_entity_id);

                    CREATE TABLE IF NOT EXISTS doc_entity_params (
                        entity_id TEXT NOT NULL,
                        ord INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        type TEXT NOT NULL DEFAULT '',
                        default_value TEXT NOT NULL DEFAULT '',
                        description TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY(entity_id, ord)
                    );
                    CREATE INDEX IF NOT EXISTS idx_doc_entity_params_entity ON doc_entity_params(entity_id);

                    CREATE TABLE IF NOT EXISTS doc_entity_notes (
                        entity_id TEXT NOT NULL,
                        ord INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        version TEXT NOT NULL DEFAULT '',
                        text TEXT NOT NULL,
                        PRIMARY KEY(entity_id, ord)
                    );
                    CREATE INDEX IF NOT EXISTS idx_doc_entity_notes_entity ON doc_entity_notes(entity_id);

                    CREATE VIRTUAL TABLE IF NOT EXISTS doc_entities_fts USING fts5(
                        entity_id UNINDEXED,
                        name,
                        qualname,
                        signature,
                        summary,
                        body_text,
                        tokenize='porter unicode61'
                    );
                    """
                )
                legacy = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pages_fts'"
                ).fetchone()
                page_cols = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(pages)").fetchall()
                }
                if "toc_json" not in page_cols:
                    conn.execute(
                        "ALTER TABLE pages ADD COLUMN toc_json TEXT NOT NULL DEFAULT '[]'"
                    )
                chunk_cols = {
                    row["name"]
                    for row in conn.execute(
                        "PRAGMA table_info(search_chunks)"
                    ).fetchall()
                }
                rebuild_chunks = False
                if "line_start" not in chunk_cols:
                    conn.execute(
                        "ALTER TABLE search_chunks ADD COLUMN line_start INTEGER NOT NULL DEFAULT 1"
                    )
                    rebuild_chunks = True
                if "line_end" not in chunk_cols:
                    conn.execute(
                        "ALTER TABLE search_chunks ADD COLUMN line_end INTEGER NOT NULL DEFAULT 1"
                    )
                    rebuild_chunks = True
                if legacy:
                    conn.executescript(
                        """
                        DROP TRIGGER IF EXISTS pages_ai;
                        DROP TRIGGER IF EXISTS pages_ad;
                        DROP TRIGGER IF EXISTS pages_au;
                        DROP TABLE IF EXISTS pages_fts;
                        """
                    )
                n_pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
                n_chunks = conn.execute(
                    "SELECT COUNT(*) FROM search_chunks"
                ).fetchone()[0]
                if n_pages and (not n_chunks or rebuild_chunks):
                    for row in conn.execute(
                        "SELECT url, title, body FROM pages"
                    ).fetchall():
                        self._replace_chunks(
                            conn, row["url"], row["title"], row["body"]
                        )
                self._rebuild_doc_entities_fts(conn)
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _replace_chunks(
        conn: sqlite3.Connection, url: str, title: str, body: str
    ) -> None:
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

    @staticmethod
    def _delete_entities_for_page(conn: sqlite3.Connection, url: str) -> None:
        entity_rows = conn.execute(
            "SELECT entity_id FROM doc_entities WHERE page_url = ?", (url,)
        ).fetchall()
        entity_ids = [row["entity_id"] for row in entity_rows]
        for entity_id in entity_ids:
            conn.execute(
                "DELETE FROM doc_entities_fts WHERE entity_id = ?", (entity_id,)
            )
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

    @staticmethod
    def _rebuild_doc_entities_fts(conn: sqlite3.Connection) -> None:
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

    @staticmethod
    def _replace_entities(
        conn: sqlite3.Connection,
        url: str,
        source_base: str,
        entities: list[dict[str, Any]],
    ) -> None:
        DocIndex._delete_entities_for_page(conn, url)
        local_to_entity: dict[str, str] = {}
        entity_ids: list[str] = []
        for i, entity in enumerate(entities):
            local_id = str(entity.get("local_id") or "")
            anchor = str(entity.get("anchor") or "").strip()
            entity_id = f"{url}#{anchor}" if anchor else f"{url}#entity-{i}"
            if local_id:
                local_to_entity[local_id] = entity_id
            entity_ids.append(entity_id)

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
                self._replace_chunks(conn, url, title, body)
                self._replace_entities(conn, url, source_base, entities or [])
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
        with self._lock:
            conn = self._connect()
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
        where_extra = ""
        extra_args: list[Any] = []
        if kind is not None:
            where_extra += " AND e.kind = ?"
            extra_args.append(kind)
        if source_base is not None:
            where_extra += " AND e.source_base = ?"
            extra_args.append(source_base)
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
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, (fts, *extra_args, limit)).fetchall()
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
                entity = self._get_entity_row(conn, entity_id)
                if entity is None:
                    return None
                if include_params:
                    entity["params"] = self._entity_params(conn, entity_id)
                if include_notes:
                    entity["notes"] = self._entity_notes(conn, entity_id)
                if include_methods and entity["kind"] == "class":
                    entity["methods"] = self._child_methods(conn, entity_id)
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
                return self._child_methods(conn, entity_id)
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

    @staticmethod
    def _get_entity_row(
        conn: sqlite3.Connection, entity_id: str
    ) -> dict[str, Any] | None:
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

    @staticmethod
    def _entity_params(
        conn: sqlite3.Connection, entity_id: str
    ) -> list[dict[str, Any]]:
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

    @staticmethod
    def _entity_notes(conn: sqlite3.Connection, entity_id: str) -> list[dict[str, Any]]:
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

    def _child_methods(
        self, conn: sqlite3.Connection, class_entity_id: str
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
            method["params"] = self._entity_params(conn, row["entity_id"])
            method["notes"] = self._entity_notes(conn, row["entity_id"])
            methods.append(method)
        return methods

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
