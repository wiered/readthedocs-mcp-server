"""URL normalization and same-site prefix checks."""

from __future__ import annotations

from urllib.parse import parse_qsl, urldefrag, urlencode, urlparse, urlunparse


def normalize_doc_root(url: str) -> str:
    """Turn a doc entry URL into a directory prefix (trailing slash)."""
    raw = url.strip()
    p = urlparse(raw)
    path = p.path or "/"
    if not path.endswith("/"):
        last = path.rsplit("/", 1)[-1]
        if "." in last:
            path = path.rsplit("/", 1)[0] + "/"
        else:
            path = path + "/"
    return urlunparse((p.scheme, p.netloc, path, "", "", ""))


def normalize_page_url(url: str) -> str:
    p = urlparse(urldefrag(url)[0])
    return urlunparse((p.scheme, p.netloc, p.path or "/", p.params, p.query, ""))


def canonical_page_url(url: str) -> str:
    """
    Normalize URLs for dedup and queueing so the same doc is not fetched twice.

    Strips Sphinx/RTD ``highlight=`` query noise and collapses ``.../index.html``
    to the directory form used on many Read the Docs builds.
    """
    u = normalize_page_url(url)
    p = urlparse(u)
    path = p.path or "/"
    low = path.lower()
    if low.endswith("/index.html"):
        # ``/dir/index.html`` → ``/dir/``
        path = path[: -len("index.html")]
    elif low == "/index.html":
        path = "/"
    pairs = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() != "highlight"
    ]
    query = urlencode(pairs)
    return urlunparse((p.scheme, p.netloc, path, p.params, query, ""))


def under_prefix(page_url: str, origin: str, path_prefix: str) -> bool:
    p = urlparse(page_url)
    if f"{p.scheme}://{p.netloc}" != origin:
        return False
    base = path_prefix.rstrip("/")
    path = (p.path or "/").rstrip("/") or "/"
    if path == base:
        return True
    return path.startswith(base + "/")
