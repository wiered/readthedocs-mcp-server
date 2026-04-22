"""Input validation for MCP tool parameters."""

from __future__ import annotations

from urllib.parse import urlparse

from readdocsserver.crawl import normalize_doc_root
from readdocsserver.utils.constants import MAX_FETCH_LINES, VALID_EDGE_TYPES


def validate_seed_url(seed_url: str) -> str:
    normalized = seed_url.strip()
    if not normalized:
        raise ValueError("seed_url must not be empty")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("seed_url must be an absolute http(s) URL")
    return normalized


def validate_max_pages(max_pages: int) -> int:
    if not 1 <= max_pages <= 5000:
        raise ValueError("max_pages must be between 1 and 5000")
    return max_pages


def validate_delay(request_delay_sec: float) -> float:
    if not 0 <= request_delay_sec <= 30:
        raise ValueError("request_delay_sec must be between 0 and 30")
    return request_delay_sec


def validate_limit(limit: int) -> int:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    return limit


def optional_entity_kind(kind: str | None) -> str | None:
    if kind is None:
        return None
    k = kind.strip().lower()
    if not k:
        return None
    if k not in {"class", "method"}:
        raise ValueError("kind must be 'class', 'method', or omitted")
    return k


def validate_symbol_name(symbol_name: str) -> str:
    s = symbol_name.strip()
    if not s:
        raise ValueError("symbol_name must not be empty")
    return s


def validate_list_pages_limit(limit: int) -> int:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    return limit


def optional_edge_types(edge_types: list[str] | None) -> list[str] | None:
    if edge_types is None:
        return None
    cleaned: list[str] = []
    for edge_type in edge_types:
        value = edge_type.strip()
        if not value:
            continue
        if value not in VALID_EDGE_TYPES:
            raise ValueError(f"Unsupported edge_type: {value}")
        if value not in cleaned:
            cleaned.append(value)
    return cleaned or None


def validate_offset(offset: int) -> int:
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if offset > 1_000_000:
        raise ValueError("offset is too large")
    return offset


def optional_source_base(source_base: str | None) -> str | None:
    if source_base is None:
        return None
    s = source_base.strip()
    if not s:
        return None
    # Match crawler-stored roots (see crawl_readthedocs: always normalize_doc_root(seed)).
    # Also accepts a concrete page URL and narrows it to the docs directory prefix.
    return normalize_doc_root(s)


def optional_url_contains(url_contains: str | None) -> str | None:
    if url_contains is None:
        return None
    s = url_contains.strip()
    return s or None


def validate_page_url(page_url: str) -> str:
    u = page_url.strip()
    if not u:
        raise ValueError("page_url must not be empty")
    parsed = urlparse(u)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("page_url must be an absolute http(s) URL")
    return u


def validate_line_slice(start: int | None, end: int | None) -> tuple[int, int] | None:
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise ValueError(
            "Provide both start and end (1-based inclusive line numbers), or omit both for the full page."
        )
    if start < 1 or end < start:
        raise ValueError("start must be >= 1 and end must be >= start.")
    if end - start + 1 > MAX_FETCH_LINES:
        raise ValueError(f"At most {MAX_FETCH_LINES} lines per fetch.")
    return (start, end)
