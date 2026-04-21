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
_MAX_REQUIRED_TERMS = 3
_AUTO_FREE_TOKEN_THRESHOLD = 5
_MAX_OPTIONAL_TERMS = 20
_MAX_FALLBACK_TERMS = 12
_LOW_SIGNAL_QUERY_TERMS = frozenset(
    {
        "docs",
        "documentation",
        "documentations",
        "document",
        "documents",
        "example",
        "examples",
        "guide",
        "guides",
        "manual",
        "reference",
        "references",
        "tutorial",
        "tutorials",
    }
)


@dataclass(frozen=True)
class ChunkSpan:
    text: str
    line_start: int
    line_end: int


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


def _is_low_signal_token(w: str) -> bool:
    return w.lower() in _LOW_SIGNAL_QUERY_TERMS


def _token_rank_key(w: str) -> tuple[int, int, int, int, int]:
    """Rank API-ish identifiers above generic prose when picking anchors."""
    return (
        0 if _is_low_signal_token(w) else 1,
        1 if ("_" in w or "." in w) else 0,
        1 if len(w) >= 4 and not w.islower() else 0,
        1 if any(ch.isdigit() for ch in w) else 0,
        len(w),
    )


def _rank_query_tokens(tokens: list[str]) -> list[str]:
    return sorted(tokens, key=_token_rank_key, reverse=True)


def _ordered_terms(tokens: list[str], selected: list[str]) -> list[str]:
    selected_keys = {item.lower() for item in selected}
    return [token for token in tokens if token.lower() in selected_keys]


def _compose_match_query(
    phrase_terms: list[str], required_tokens: list[str], optional_tokens: list[str]
) -> str | None:
    parts: list[str] = []
    parts.extend(phrase_terms)
    if required_tokens:
        parts.append(" AND ".join(_quote_token(token) for token in required_tokens))
    if optional_tokens:
        parts.append(
            "(" + " OR ".join(_quote_token(token) for token in optional_tokens) + ")"
        )
    return " AND ".join(parts) if parts else None


def _dedupe_queries(queries: list[str | None]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for query in queries:
        if query is None:
            continue
        cleaned = query.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def _auto_free_mode(phrases: list[str], tokens: list[str]) -> bool:
    return len(phrases) + len(tokens) >= _AUTO_FREE_TOKEN_THRESHOLD


def _pick_required_tokens(
    tokens: list[str], strong: list[str], free_mode: bool
) -> list[str]:
    ranked_pool = _rank_query_tokens(strong or tokens)
    ranked_pool = [
        token for token in ranked_pool if not _is_low_signal_token(token)
    ] or ranked_pool
    if not ranked_pool:
        return []
    required_budget = min(_MAX_REQUIRED_TERMS, len(ranked_pool))
    if free_mode and len(ranked_pool) > 1:
        required_budget = min(required_budget, 2)
    elif len(ranked_pool) >= 2:
        required_budget = max(2, required_budget)
    return ranked_pool[:required_budget]


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
            w = w.strip("\"',.;:!?()[]")
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


def _fts_match_stages(user_query: str) -> list[str]:
    """
    Build progressively looser FTS5 MATCH strings.

    Stage 1: require a small set of high-signal tokens and keep the rest optional.
    Stage 2: keep a single anchor token plus optional context.
    Stage 3: broad OR fallback, enabled automatically for longer free-form queries.
    """
    stripped = user_query.strip()
    if not stripped:
        return []
    phrases, tokens = _collect_query_pieces(stripped)
    if not phrases and not tokens:
        return []

    phrase_terms = [_quote_token(p) for p in phrases]
    strong = [t for t in tokens if _is_strong_token(t)]
    free_mode = _auto_free_mode(phrases, tokens)

    required = _ordered_terms(tokens, _pick_required_tokens(tokens, strong, free_mode))
    req_set = {token.lower() for token in required}
    optional = [token for token in tokens if token.lower() not in req_set][
        :_MAX_OPTIONAL_TERMS
    ]

    stages: list[str | None] = [
        _compose_match_query(phrase_terms, required, optional),
    ]

    if required and optional:
        anchor = required[:1]
        anchor_set = {token.lower() for token in anchor}
        anchor_optional = [
            token for token in tokens if token.lower() not in anchor_set
        ][:_MAX_OPTIONAL_TERMS]
        stages.append(_compose_match_query(phrase_terms, anchor, anchor_optional))

    fallback_tokens = _rank_query_tokens(tokens)[:_MAX_FALLBACK_TERMS]
    if phrases and fallback_tokens:
        stages.append(
            " AND ".join(phrase_terms)
            + " AND ("
            + " OR ".join(_quote_token(token) for token in fallback_tokens)
            + ")"
        )
    elif phrases:
        stages.append(" AND ".join(phrase_terms))
    elif free_mode and fallback_tokens:
        stages.append(" OR ".join(_quote_token(token) for token in fallback_tokens))

    return _dedupe_queries(stages)


def _fts_match_queries(user_query: str) -> tuple[str | None, str | None]:
    stages = _fts_match_stages(user_query)
    if not stages:
        return None, None
    primary = stages[0]
    fallback = stages[-1] if len(stages) > 1 else None
    if fallback == primary:
        fallback = None
    return primary, fallback


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


def _hard_split_span(span: ChunkSpan, max_chars: int, overlap: int) -> list[ChunkSpan]:
    if len(span.text) <= max_chars:
        return [span]

    def scaled_line(char_pos: int) -> int:
        if span.line_end <= span.line_start or len(span.text) <= 1:
            return span.line_start
        clamped = min(max(char_pos, 0), len(span.text) - 1)
        frac = clamped / (len(span.text) - 1)
        delta = span.line_end - span.line_start
        return min(span.line_end, span.line_start + int(round(delta * frac)))

    out: list[ChunkSpan] = []
    start = 0
    while start < len(span.text):
        end = min(start + max_chars, len(span.text))
        chunk_text = span.text[start:end]
        if "\n" in span.text:
            chunk_start = span.line_start + span.text[:start].count("\n")
            chunk_end = chunk_start + chunk_text.count("\n")
        else:
            chunk_start = scaled_line(start)
            chunk_end = max(chunk_start, scaled_line(max(start, end - 1)))
        out.append(ChunkSpan(chunk_text, chunk_start, chunk_end))
        if end >= len(span.text):
            break
        start = max(0, end - overlap)
    return out


def _merge_paragraphs(
    paragraphs: list[str], max_chars: int, min_merge: int
) -> list[str]:
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


def _merge_spans(
    parts: list[ChunkSpan], max_chars: int, min_merge: int
) -> list[ChunkSpan]:
    chunks: list[ChunkSpan] = []
    buf: list[ChunkSpan] = []
    size = 0
    for part in parts:
        add_len = len(part.text) + (2 if buf else 0)
        if buf and size + add_len > max_chars and size >= min_merge:
            chunks.append(
                ChunkSpan(
                    text="\n\n".join(item.text for item in buf),
                    line_start=buf[0].line_start,
                    line_end=buf[-1].line_end,
                )
            )
            buf = [part]
            size = len(part.text)
        else:
            buf.append(part)
            size += add_len
    if buf:
        chunks.append(
            ChunkSpan(
                text="\n\n".join(item.text for item in buf),
                line_start=buf[0].line_start,
                line_end=buf[-1].line_end,
            )
        )
    return chunks


def _body_to_chunks_with_lines(body: str) -> list[ChunkSpan]:
    text = body.strip()
    raw_lines = body.splitlines()
    if not text:
        return [ChunkSpan("", 1, 1)]

    paras: list[ChunkSpan] = []
    buf: list[str] = []
    start_line: int | None = None
    end_line: int | None = None
    for lineno, raw_line in enumerate(raw_lines, start=1):
        if raw_line.strip():
            if start_line is None:
                start_line = lineno
            buf.append(raw_line)
            end_line = lineno
            continue
        if buf:
            paras.append(
                ChunkSpan(
                    text="\n".join(buf).strip(),
                    line_start=start_line or lineno,
                    line_end=end_line or lineno,
                )
            )
            buf = []
            start_line = None
            end_line = None
    if buf:
        paras.append(
            ChunkSpan(
                text="\n".join(buf).strip(),
                line_start=start_line or 1,
                line_end=end_line or max(1, len(raw_lines)),
            )
        )
    if not paras:
        return [ChunkSpan(text, 1, max(1, len(raw_lines)))]

    # Single block with mostly single newlines (common Sphinx output): merge lines.
    if len(paras) == 1 and paras[0].text.count("\n") > 8 and "\n\n" not in text:
        single = paras[0]
        line_items = [
            (lineno, raw_line.strip())
            for lineno, raw_line in enumerate(raw_lines, start=1)
            if single.line_start <= lineno <= single.line_end and raw_line.strip()
        ]
        pseudo: list[ChunkSpan] = []
        pseudo_buf: list[str] = []
        pseudo_start: int | None = None
        pseudo_end: int | None = None
        pseudo_size = 0
        for lineno, line_text in line_items:
            add = len(line_text) + (1 if pseudo_buf else 0)
            if (
                pseudo_buf
                and pseudo_size + add > _CHUNK_MIN_MERGE
                and pseudo_size + add > _CHUNK_MAX * 0.9
            ):
                pseudo.append(
                    ChunkSpan(
                        text=" ".join(pseudo_buf),
                        line_start=pseudo_start or lineno,
                        line_end=pseudo_end or lineno,
                    )
                )
                pseudo_buf = [line_text]
                pseudo_start = lineno
                pseudo_end = lineno
                pseudo_size = len(line_text)
            else:
                if pseudo_start is None:
                    pseudo_start = lineno
                pseudo_buf.append(line_text)
                pseudo_end = lineno
                pseudo_size += add
        if pseudo_buf:
            pseudo.append(
                ChunkSpan(
                    text=" ".join(pseudo_buf),
                    line_start=pseudo_start or 1,
                    line_end=pseudo_end or max(1, len(raw_lines)),
                )
            )
        paras = pseudo

    merged = _merge_spans(paras, _CHUNK_MAX, _CHUNK_MIN_MERGE)
    out: list[ChunkSpan] = []
    for merged_span in merged:
        if len(merged_span.text) <= _CHUNK_MAX:
            out.append(merged_span)
        else:
            out.extend(_hard_split_span(merged_span, _CHUNK_MAX, _CHUNK_OVERLAP))
    return out if out else [ChunkSpan(text[:_CHUNK_MAX], 1, max(1, len(raw_lines)))]


def _body_to_chunks(body: str) -> list[str]:
    """Split page text into overlapping segments so FTS BM25 is not dominated by huge pages."""
    return [span.text for span in _body_to_chunks_with_lines(body)]


_CONTEXT_LINE_MAX = 800


def _query_match_needles(query: str) -> list[str]:
    """Tokens (and quoted-phrase words) used to pick the best single line from a hit chunk."""
    phrases, tokens = _collect_query_pieces(query)
    needles: list[str] = []
    for t in tokens:
        needles.append(t)
    for ph in phrases:
        for w in re.findall(r"[^\s]+", ph):
            w = w.strip("\"',.;:!?()[]")
            if len(w) < 2:
                continue
            needles.append(w)
    seen: set[str] = set()
    out: list[str] = []
    for n in needles:
        k = n.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(n)
    return out


def _context_line_from_chunk(chunk: str, query: str) -> str:
    """One approximate source line from the matched chunk (subset of the stored page body)."""
    text = chunk.strip()
    if not text:
        return ""
    needles = _query_match_needles(query)
    lines = [(i, ln.strip()) for i, ln in enumerate(text.splitlines()) if ln.strip()]
    if not lines:
        return text[:_CONTEXT_LINE_MAX]
    if not needles:
        _, pick = max(lines, key=lambda item: len(item[1]))
        return pick[:_CONTEXT_LINE_MAX]

    def score(line: str) -> int:
        low = line.lower()
        return sum(1 for n in needles if n.lower() in low)

    best_s = max(score(ln) for _, ln in lines)
    candidates = [(i, ln) for i, ln in lines if score(ln) == best_s and best_s > 0]
    if not candidates:
        _, pick = max(lines, key=lambda item: len(item[1]))
    else:
        _, pick = max(candidates, key=lambda item: len(item[1]))
    return pick[:_CONTEXT_LINE_MAX]


def _context_line_info_from_chunk(chunk: str, query: str) -> tuple[str, int | None]:
    """Return the best context line plus its 0-based relative line offset inside the chunk."""
    text = chunk.strip()
    if not text:
        return "", None
    needles = _query_match_needles(query)
    lines = [(i, ln.strip()) for i, ln in enumerate(text.splitlines()) if ln.strip()]
    if not lines:
        return text[:_CONTEXT_LINE_MAX], 0
    if not needles:
        rel, pick = max(lines, key=lambda item: len(item[1]))
        return pick[:_CONTEXT_LINE_MAX], rel

    def score(line: str) -> int:
        low = line.lower()
        return sum(1 for n in needles if n.lower() in low)

    best_s = max(score(ln) for _, ln in lines)
    candidates = [(i, ln) for i, ln in lines if score(ln) == best_s and best_s > 0]
    if not candidates:
        rel, pick = max(lines, key=lambda item: len(item[1]))
    else:
        rel, pick = max(candidates, key=lambda item: len(item[1]))
    return pick[:_CONTEXT_LINE_MAX], rel


def _approx_line_in_body(
    body: str,
    line_start: int,
    line_end: int,
    query: str,
    snippet: str,
) -> int | None:
    """Best-effort absolute line lookup for a hit window inside the stored page body."""
    lines = body.splitlines()
    if not lines:
        return None
    start = max(1, min(line_start, len(lines)))
    end = max(start, min(line_end, len(lines)))
    candidates = [
        (lineno, lines[lineno - 1].strip()) for lineno in range(start, end + 1)
    ]
    non_empty = [(lineno, text) for lineno, text in candidates if text]
    if non_empty:
        candidates = non_empty
    if not candidates:
        return start

    seen: set[str] = set()
    needles: list[str] = []
    for item in [*_query_match_needles(query), *_query_match_needles(snippet)]:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        needles.append(item)
    snippet_low = snippet.lower()

    def score(item: tuple[int, str]) -> tuple[int, int]:
        _, line = item
        low = line.lower()
        exact = 1000 if snippet_low and snippet_low in low else 0
        matches = sum(100 for needle in needles if needle.lower() in low)
        return exact + matches, len(line)

    best = max(candidates, key=score)
    if score(best)[0] <= 0:
        return start
    return best[0]


@dataclass
class SearchHit:
    url: str
    title: str
    snippet: str
    rank: float
    line: int | None = None
    chunk_line_start: int | None = None
    chunk_line_end: int | None = None


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
                    """
                )
                legacy = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pages_fts'"
                ).fetchone()
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

    def clear_source(self, source_base: str) -> int:
        with self._lock:
            conn = self._connect()
            try:
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
        self, url: str, title: str, body: str, source_base: str, fetched_at: int
    ) -> None:
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
                    SELECT url, title, source_base
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
                    }
                    for r in rows
                ], total
            finally:
                conn.close()

    def stats_json(self) -> str:
        return json.dumps({"sources": self.list_sources(), "db_path": str(self._path)})
