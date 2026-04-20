"""SQLite FTS5 index for crawled documentation pages."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def default_db_path() -> Path:
    return Path.home() / ".cache" / "readdocs-mcp" / "index.sqlite"


def _fts_match_query(user_query: str) -> str | None:
    """Build a safe FTS5 MATCH string from natural language."""
    words = re.findall(r"[^\s]+", user_query.strip())
    tokens: list[str] = []
    for w in words:
        w = w.strip('"\',.;:!?()[]')
        if len(w) < 2:
            continue
        safe = w.replace('"', '""')
        tokens.append(f'"{safe}"')
    if not tokens:
        return None
    return " AND ".join(tokens)


@dataclass
class SearchHit:
    url: str
    title: str
    snippet: str
    rank: float


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
                        fetched_at INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_pages_source ON pages(source_base);
                    CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
                        url UNINDEXED,
                        title,
                        body,
                        content='pages',
                        content_rowid='rowid',
                        tokenize='porter unicode61'
                    );
                    CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
                        INSERT INTO pages_fts(rowid, url, title, body)
                        VALUES (new.rowid, new.url, new.title, new.body);
                    END;
                    CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
                        INSERT INTO pages_fts(pages_fts, rowid, url, title, body)
                        VALUES('delete', old.rowid, old.url, old.title, old.body);
                    END;
                    CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
                        INSERT INTO pages_fts(pages_fts, rowid, url, title, body)
                        VALUES('delete', old.rowid, old.url, old.title, old.body);
                        INSERT INTO pages_fts(rowid, url, title, body)
                        VALUES (new.rowid, new.url, new.title, new.body);
                    END;
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def clear_source(self, source_base: str) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM pages WHERE source_base = ?", (source_base,))
                deleted = cur.rowcount or 0
                conn.commit()
                return deleted
            finally:
                conn.close()

    def upsert_page(self, url: str, title: str, body: str, source_base: str, fetched_at: int) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO pages(url, title, body, source_base, fetched_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        title=excluded.title,
                        body=excluded.body,
                        source_base=excluded.source_base,
                        fetched_at=excluded.fetched_at
                    """,
                    (url, title, body, source_base, fetched_at),
                )
                conn.commit()
            finally:
                conn.close()

    def search(self, query: str, limit: int = 15) -> list[SearchHit]:
        fts = _fts_match_query(query)
        if fts is None:
            return []
        limit = max(1, min(limit, 100))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT p.url, p.title,
                           snippet(pages_fts, 2, '[', ']', ' … ', 24) AS snip,
                           bm25(pages_fts) AS rnk
                    FROM pages_fts
                    JOIN pages AS p ON pages_fts.rowid = p.rowid
                    WHERE pages_fts MATCH ?
                    ORDER BY rnk
                    LIMIT ?
                    """,
                    (fts, limit),
                ).fetchall()
                return [
                    SearchHit(
                        url=row["url"],
                        title=row["title"],
                        snippet=row["snip"],
                        rank=float(row["rnk"]),
                    )
                    for row in rows
                ]
            except sqlite3.OperationalError:
                return []
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

    def stats_json(self) -> str:
        return json.dumps({"sources": self.list_sources(), "db_path": str(self._path)})
