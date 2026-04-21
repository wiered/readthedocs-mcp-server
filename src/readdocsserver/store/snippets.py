"""Pick snippet lines and approximate absolute line numbers for hits."""

from __future__ import annotations

import re

from .constants import _CONTEXT_LINE_MAX
from .fts_query import _collect_query_pieces


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
