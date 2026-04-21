"""SQLite DDL, triggers, and migrations for DocIndex."""

from __future__ import annotations

import sqlite3

from .operations import rebuild_doc_entities_fts, replace_chunks

_INIT_SCRIPT = """
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


def init_schema(conn: sqlite3.Connection) -> None:
    """Apply schema, run migrations, rebuild derived data. Caller commits and closes."""
    conn.executescript(_INIT_SCRIPT)
    legacy = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pages_fts'"
    ).fetchone()
    page_cols = {row["name"] for row in conn.execute("PRAGMA table_info(pages)").fetchall()}
    if "toc_json" not in page_cols:
        conn.execute(
            "ALTER TABLE pages ADD COLUMN toc_json TEXT NOT NULL DEFAULT '[]'"
        )
    chunk_cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(search_chunks)").fetchall()
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
    n_chunks = conn.execute("SELECT COUNT(*) FROM search_chunks").fetchone()[0]
    if n_pages and (not n_chunks or rebuild_chunks):
        for row in conn.execute("SELECT url, title, body FROM pages").fetchall():
            replace_chunks(conn, row["url"], row["title"], row["body"])
    rebuild_doc_entities_fts(conn)
    conn.commit()
