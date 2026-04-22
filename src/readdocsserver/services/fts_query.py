"""Build FTS5 MATCH strings from a natural-language query."""

from __future__ import annotations

import re

from readdocsserver.services.constants import (
    _AUTO_FREE_TOKEN_THRESHOLD,
    _LOW_SIGNAL_QUERY_TERMS,
    _MAX_FALLBACK_TERMS,
    _MAX_OPTIONAL_TERMS,
    _MAX_REQUIRED_TERMS,
    _STOPWORDS,
)


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
