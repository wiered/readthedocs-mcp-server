"""SQLite FTS5 index for crawled documentation pages."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Common English doc / NL filler — AND-ing these hurts recall on long questions.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "might",
        "must",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "should",
        "so",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "too",
        "use",
        "using",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)

_CHUNK_MAX = 2200
_CHUNK_OVERLAP = 180
_CHUNK_MIN_MERGE = 380
# AND-ing many rare terms yields empty hits; cap required terms.
_MAX_AND_TERMS = 6


def default_db_path() -> Path:
    return Path.home() / ".cache" / "readdocs-mcp" / "index.sqlite"


def _fts_escape(s: str) -> str:
    return s.replace('"', '""')


def _quote_token(s: str) -> str:
    return f'"{_fts_escape(s)}"'


def _is_strong_token(w: str) -> bool:
    """Identifiers, versions, and long / mixed-case tokens are kept as required AND terms."""
    if len(w) >= 6:
        return True
    if "_" in w or "." in w:
        return True
    if any(ch.isdigit() for ch in w):
        return True
    if len(w) >= 4 and not w.islower():
        return True
    return False


def _collect_query_pieces(raw: str) -> tuple[list[str], list[str]]:
    """Split into quoted phrases (exact FTS phrases) and remaining whitespace tokens."""
    phrases: list[str] = []
    rest_parts: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        if raw[i] == '"':
            j = raw.find('"', i + 1)
            if j == -1:
                rest_parts.append(raw[i:])
                break
            inner = raw[i + 1 : j].strip()
            if inner:
                phrases.append(inner)
            i = j + 1
            continue
        j = i
        while j < n and raw[j] != '"':
            j += 1
        piece = raw[i:j].strip()
        if piece:
            rest_parts.append(piece)
        i = j
    tokens: list[str] = []
    for piece in rest_parts:
        for w in re.findall(r"[^\s]+", piece):
            w = w.strip('"\',.;:!?()[]')
            if len(w) < 2:
                continue
            low = w.lower()
            if low in _STOPWORDS:
                continue
            tokens.append(w)
    # de-dupe tokens case-insensitively, preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for w in tokens:
        k = w.lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(w)
    return phrases, uniq


def _fts_match_queries(user_query: str) -> tuple[str | None, str | None]:
    """
    Build primary and optional fallback FTS5 MATCH strings.

    Primary: required AND on selective tokens + quoted phrases; extra weak terms OR-grouped
    when both strong and weak tokens exist.

    Fallback: OR across tokens (bounded) for recall when AND is too strict.
    """
    stripped = user_query.strip()
    if not stripped:
        return None, None
    phrases, tokens = _collect_query_pieces(stripped)
    if not phrases and not tokens:
        return None, None

    phrase_terms = [_quote_token(p) for p in phrases]

    strong = [t for t in tokens if _is_strong_token(t)]
    weak = [t for t in tokens if not _is_strong_token(t)]

    if strong:
        strong.sort(key=len, reverse=True)
        required = strong[:_MAX_AND_TERMS]
    else:
        tokens_by_len = sorted(tokens, key=len, reverse=True)
        required = tokens_by_len[: min(_MAX_AND_TERMS, max(2, len(tokens_by_len)))]

    req_set = {t.lower() for t in required}
    weak = [w for w in weak if w.lower() not in req_set]

    primary_parts: list[str] = []
    primary_parts.extend(phrase_terms)
    if required:
        primary_parts.append(" AND ".join(_quote_token(t) for t in required))
    if weak and required:
        or_clause = " OR ".join(_quote_token(w) for w in weak[:20])
        primary_parts.append(f"({or_clause})")

    if not primary_parts:
        primary_parts = [_quote_token(t) for t in tokens[:_MAX_AND_TERMS]]

    primary = " AND ".join(primary_parts) if primary_parts else None

    # Fallback: OR of longest tokens; optional AND with quoted phrases when phrases exist.
    fallback_tokens = sorted(set(tokens), key=len, reverse=True)[:12]
    if phrases and fallback_tokens:
        fb = " AND ".join(phrase_terms) + " AND (" + " OR ".join(_quote_token(t) for t in fallback_tokens) + ")"
    elif phrases:
        fb = " AND ".join(phrase_terms) if len(phrase_terms) > 1 else phrase_terms[0]
    else:
        fb = " OR ".join(_quote_token(t) for t in fallback_tokens) if fallback_tokens else None

    if fb == primary:
        fb = None
    return primary, fb


def _hard_split(text: str, max_chars: int, overlap: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        out.append(text[start:end])
        if end >= len(text):
            break
        start = max(0, end - overlap)
    return out


def _merge_paragraphs(paragraphs: list[str], max_chars: int, min_merge: int) -> list[str]:
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for p in paragraphs:
        add_len = len(p) + (2 if buf else 0)
        if buf and size + add_len > max_chars and size >= min_merge:
            chunks.append("\n\n".join(buf))
            buf = [p]
            size = len(p)
        else:
            buf.append(p)
            size += add_len
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks


def _body_to_chunks(body: str) -> list[str]:
    """Split page text into overlapping segments so FTS BM25 is not dominated by huge pages."""
    text = body.strip()
    if not text:
        return [""]
    paras = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    if not paras:
        return _hard_split(text, _CHUNK_MAX, _CHUNK_OVERLAP)
    # Single block with mostly single newlines (common Sphinx output): merge lines.
    if len(paras) == 1 and paras[0].count("\n") > 8 and "\n\n" not in text:
        lines = [ln.strip() for ln in paras[0].split("\n") if ln.strip()]
        pseudo: list[str] = []
        buf: list[str] = []
        sz = 0
        for ln in lines:
            add = len(ln) + (1 if buf else 0)
            if buf and sz + add > _CHUNK_MIN_MERGE and sz + add > _CHUNK_MAX * 0.9:
                pseudo.append(" ".join(buf))
                buf = [ln]
                sz = len(ln)
            else:
                buf.append(ln)
                sz += add
        if buf:
            pseudo.append(" ".join(buf))
        paras = pseudo
    merged = _merge_paragraphs(paras, _CHUNK_MAX, _CHUNK_MIN_MERGE)
    out: list[str] = []
    for m in merged:
        if len(m) <= _CHUNK_MAX:
            out.append(m)
        else:
            out.extend(_hard_split(m, _CHUNK_MAX, _CHUNK_OVERLAP))
    return out if out else [text[:_CHUNK_MAX]]


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

                    CREATE TABLE IF NOT EXISTS search_chunks (
                        chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT NOT NULL,
                        ord INTEGER NOT NULL,
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
                    """
                )
                legacy = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pages_fts'"
                ).fetchone()
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
                if n_pages and not n_chunks:
                    for row in conn.execute("SELECT url, title, body FROM pages").fetchall():
                        self._replace_chunks(conn, row["url"], row["title"], row["body"])
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _replace_chunks(conn: sqlite3.Connection, url: str, title: str, body: str) -> None:
        conn.execute("DELETE FROM search_chunks WHERE url = ?", (url,))
        parts = _body_to_chunks(body)
        for i, ch in enumerate(parts):
            conn.execute(
                "INSERT INTO search_chunks(url, ord, title, chunk) VALUES (?, ?, ?, ?)",
                (url, i, title, ch),
            )

    def clear_source(self, source_base: str) -> int:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM search_chunks WHERE url IN (SELECT url FROM pages WHERE source_base = ?)",
                    (source_base,),
                )
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
                conn.execute("BEGIN IMMEDIATE")
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
                self._replace_chunks(conn, url, title, body)
                conn.commit()
            finally:
                conn.close()

    def search(self, query: str, limit: int = 15) -> list[SearchHit]:
        primary, fallback = _fts_match_queries(query)
        if primary is None:
            return []
        limit = max(1, min(limit, 100))
        return self._search_fts(primary, limit) or (
            self._search_fts(fallback, limit) if fallback else []
        )

    def _search_fts(self, fts: str, limit: int) -> list[SearchHit]:
        # snippet() must run in a SELECT that uses MATCH on the fts table directly
        # (not inside a CTE/window), or SQLite raises "unable to use function snippet".
        fetch_cap = min(1000, max(200, limit * 40))
        sql = """
            SELECT p.url AS url,
                   p.title AS title,
                   snippet(search_chunks_fts, 2, '[', ']', ' … ', 32) AS snip,
                   bm25(search_chunks_fts) AS rnk
            FROM search_chunks_fts
            JOIN search_chunks AS sc ON search_chunks_fts.rowid = sc.chunk_id
            JOIN pages AS p ON p.url = sc.url
            WHERE search_chunks_fts MATCH ?
            ORDER BY bm25(search_chunks_fts) ASC, p.url ASC
            LIMIT ?
        """
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, (fts, fetch_cap)).fetchall()
            except sqlite3.OperationalError:
                return []
            finally:
                conn.close()
        seen: set[str] = set()
        out: list[SearchHit] = []
        for row in rows:
            url = row["url"]
            if url in seen:
                continue
            seen.add(url)
            out.append(
                SearchHit(
                    url=url,
                    title=row["title"],
                    snippet=row["snip"],
                    rank=float(row["rnk"]),
                )
            )
            if len(out) >= limit:
                break
        return out

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
