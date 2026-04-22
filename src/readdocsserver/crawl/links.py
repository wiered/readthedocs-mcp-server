"""Collect same-site links from crawled HTML pages."""

from __future__ import annotations

from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from readdocsserver.crawl.urls import normalize_page_url, under_prefix

SKIP_EXTENSIONS = (
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp4",
    ".webm",
    ".js",
    ".css",
)

_HEAD_RELS = frozenset(
    {
        "next",
        "prev",
        "up",
        "chapter",
        "first",
        "last",
        "index",
        "appendix",
        "help",
        "contents",
        "toc",
        "start",
        "top",
        "subsection",
        "section",
        "bookmark",
    }
)


def same_site_links(
    html: bytes, base_url: str, origin: str, path_prefix: str
) -> list[str]:
    """Collect same-site doc URLs from anchors and Sphinx/RTD ``<link rel="...">`` navigation."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []

    def consider_href(href: str) -> None:
        joined = normalize_page_url(urljoin(base_url, href))
        p = urlparse(joined)
        if p.scheme not in ("http", "https"):
            return
        low = joined.lower()
        if any(low.endswith(ext) for ext in SKIP_EXTENSIONS):
            return
        if under_prefix(joined, origin, path_prefix):
            out.append(joined)

    for a in soup.find_all("a", href=True):
        consider_href(a["href"])
    for link in soup.find_all("link", href=True):
        rel = link.get("rel")
        if not rel:
            continue
        rel_parts = {str(r).lower() for r in (rel if isinstance(rel, list) else [rel])}
        if rel_parts & _HEAD_RELS:
            consider_href(link["href"])
    return out
