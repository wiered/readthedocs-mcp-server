"""Crawl Read the Docs / Sphinx HTML under a documentation root URL."""

from __future__ import annotations

import asyncio
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

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


_SITEMAP_NS_RE = re.compile(r"^\{([^}]+)\}")


def _sitemap_tag_local_name(tag: str) -> str:
    m = _SITEMAP_NS_RE.match(tag)
    return tag[m.end() :] if m else tag


def _parse_sitemap_urls(xml_bytes: bytes) -> tuple[list[str], list[str]]:
    """
    Return (page_locs, nested_sitemap_locs) from a sitemap or sitemap index document.
    """
    page_locs: list[str] = []
    nested: list[str] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return page_locs, nested
    root_name = _sitemap_tag_local_name(root.tag).lower()
    for el in root.iter():
        name = _sitemap_tag_local_name(el.tag).lower()
        if name != "loc":
            continue
        text = (el.text or "").strip()
        if not text:
            continue
        if root_name == "sitemapindex":
            nested.append(text)
        else:
            page_locs.append(text)
    return page_locs, nested


async def discover_sitemap_seed_urls(
    client: AsyncSession,
    doc_root: str,
    origin: str,
    path_prefix: str,
    *,
    max_urls: int,
) -> list[str]:
    """
    Pull URLs from ``sitemap.xml`` (and one level of sitemap index) under ``doc_root``.

    Sphinx often emits a complete sitemap; seeding the crawl avoids missing deep pages
    when ``max_pages`` is modest and the sidebar lists hundreds of siblings before
    links to deeper sections appear in the HTML parse order.
    """
    out: list[str] = []
    seen: set[str] = set()
    to_fetch = [urljoin(doc_root, "sitemap.xml")]
    index_fetches = 0
    while to_fetch and len(out) < max_urls and index_fetches < 8:
        sm_url = to_fetch.pop(0)
        c_sm = canonical_page_url(sm_url)
        if c_sm in seen:
            continue
        seen.add(c_sm)
        index_fetches += 1
        try:
            resp = await client.get(sm_url, timeout=30, allow_redirects=True)
        except Exception:
            continue
        if resp.status_code >= 400:
            continue
        ctype = resp.headers.get("content-type", "").lower()
        raw = resp.content.lstrip()
        looks_like_sitemap = (
            raw.startswith(b"<?xml")
            or raw.startswith(b"<urlset")
            or raw.startswith(b"<sitemapindex")
        )
        if (
            "xml" not in ctype
            and not sm_url.lower().endswith(".xml")
            and not looks_like_sitemap
        ):
            continue
        pages, nested = _parse_sitemap_urls(resp.content)
        for n in nested:
            cn = canonical_page_url(n)
            if cn not in seen and len(to_fetch) < 32:
                to_fetch.append(n)
        for loc in pages:
            joined = normalize_page_url(loc)
            if not under_prefix(joined, origin, path_prefix):
                continue
            cj = canonical_page_url(joined)
            if cj in seen:
                continue
            seen.add(cj)
            out.append(joined)
            if len(out) >= max_urls:
                break
    return out


def under_prefix(page_url: str, origin: str, path_prefix: str) -> bool:
    p = urlparse(page_url)
    if f"{p.scheme}://{p.netloc}" != origin:
        return False
    base = path_prefix.rstrip("/")
    path = (p.path or "/").rstrip("/") or "/"
    if path == base:
        return True
    return path.startswith(base + "/")


def extract_text_and_title(html: bytes, page_url: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title_el = soup.find("title")
    title = title_el.get_text(strip=True) if title_el else ""
    main = None
    for sel in (
        "div.rst-content",
        "div[itemprop='articleBody']",
        "div.document",
        "article",
        "main",
        '[role="main"]',
    ):
        main = soup.select_one(sel)
        if main:
            break
    if main is None:
        main = soup.body or soup
    text = main.get_text("\n", strip=True)
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else urlparse(page_url).path
    return title, text


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


async def crawl_readthedocs(
    seed_url: str,
    *,
    max_pages: int = 200,
    request_delay_sec: float = 0.15,
    impersonate: str | None = None,
    on_page=None,
) -> dict:
    """
    BFS crawl starting from seed_url, staying under the documentation root directory.

    on_page: optional async callback(url, title, body) for each stored page.
    """
    root = normalize_doc_root(seed_url)
    p = urlparse(root)
    origin = f"{p.scheme}://{p.netloc}"
    path_prefix = p.path if p.path.endswith("/") else p.path + "/"

    seed = normalize_page_url(seed_url.strip())
    if under_prefix(seed, origin, path_prefix):
        first = seed
    else:
        first = normalize_page_url(urljoin(root, "index.html"))

    visited: set[str] = set()
    queued: set[str] = set()
    queue: deque[str] = deque()

    def try_enqueue(raw_url: str) -> None:
        ju = normalize_page_url(raw_url.strip())
        if not under_prefix(ju, origin, path_prefix):
            return
        cc = canonical_page_url(ju)
        if cc in visited or cc in queued:
            return
        queued.add(cc)
        queue.append(ju)

    stats: dict = {
        "source_base": root,
        "fetched": 0,
        "skipped": 0,
        "errors": [],
        "sitemap_seeds": 0,
    }

    tls_profile = impersonate or os.environ.get("READTHEDOCS_MCP_IMPERSONATE", "chrome")
    # curl_cffi impersonates a real browser TLS stack; many RTD sites sit behind
    # Cloudflare and return 403 to generic Python HTTP clients (e.g. httpx/requests).
    async with AsyncSession(impersonate=tls_profile) as client:
        sitemap_cap = min(max(max_pages * 8, 500), 20000)
        sitemap_urls = await discover_sitemap_seed_urls(
            client,
            root,
            origin,
            path_prefix,
            max_urls=sitemap_cap,
        )
        stats["sitemap_seeds"] = len(sitemap_urls)

        try_enqueue(first)
        for u in sitemap_urls:
            try_enqueue(u)

        while queue and stats["fetched"] < max_pages:
            current = queue.popleft()
            cc = canonical_page_url(current)
            queued.discard(cc)
            if cc in visited:
                continue
            visited.add(cc)
            fetch_url = cc

            await asyncio.sleep(request_delay_sec)

            try:
                resp = await client.get(fetch_url, timeout=30, allow_redirects=True)
            except Exception as e:
                stats["errors"].append({"url": fetch_url, "error": str(e)})
                continue

            ctype = resp.headers.get("content-type", "").lower()
            if resp.status_code >= 400:
                stats["errors"].append(
                    {"url": fetch_url, "error": f"HTTP {resp.status_code}"}
                )
                continue
            if "text/html" not in ctype and not fetch_url.endswith((".html", "/")):
                stats["skipped"] += 1
                continue

            title, body = extract_text_and_title(resp.content, str(resp.url))
            final_url = canonical_page_url(normalize_page_url(str(resp.url)))
            if not under_prefix(final_url, origin, path_prefix):
                stats["skipped"] += 1
                continue

            stats["fetched"] += 1
            fetched_at = int(time.time())
            if on_page:
                await on_page(final_url, title, body, root, fetched_at)

            for link in same_site_links(
                resp.content, str(resp.url), origin, path_prefix
            ):
                try_enqueue(link)

    return stats
