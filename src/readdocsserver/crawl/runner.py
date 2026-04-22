"""BFS crawl over Read the Docs / Sphinx HTML under a documentation root."""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

from readdocsserver.crawl.entities import extract_structured_entities
from readdocsserver.crawl.links import same_site_links
from readdocsserver.crawl.render import extract_text_and_title, _find_main_content
from readdocsserver.crawl.sitemap import discover_sitemap_seed_urls
from readdocsserver.crawl.toc import extract_page_toc
from readdocsserver.crawl.urls import (
    canonical_page_url,
    normalize_doc_root,
    normalize_page_url,
    under_prefix,
)


def _decompose_soup(html) -> BeautifulSoup:
    """Decompose soup."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for tag in soup.select("a.headerlink"):
        tag.decompose()

    return soup


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

    on_page: optional async callback(url, title, body, source_base, fetched_at, entities)
    for each stored page.
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
        # Keep sitemap URL list modest; URLs are queued after the first successful
        # HTML fetch (and same-site links from that page) so TOC/sidebar wins over
        # hundreds of alphabetically ordered sitemap entries.
        sitemap_cap = min(max(max_pages * 8, 120), max(max_pages * 40, 400), 5000)
        sitemap_urls = await discover_sitemap_seed_urls(
            client,
            root,
            origin,
            path_prefix,
            max_urls=sitemap_cap,
        )
        stats["sitemap_seeds"] = len(sitemap_urls)

        try_enqueue(first)
        sitemap_seeded = False

        def _enqueue_sitemap_batch() -> None:
            nonlocal sitemap_seeded
            if sitemap_seeded or not sitemap_urls:
                return
            for u in sitemap_urls:
                try_enqueue(u)
            sitemap_seeded = True

        # Hard cap so a bad network / mostly-404 queue cannot spin for hours.
        max_dequeues = min(
            25000,
            max(len(sitemap_urls) + 2000, max_pages * 600),
        )
        dequeues = 0

        while stats["fetched"] < max_pages:
            print(f"stats['fetched']: {stats['fetched']}")
            if not queue:
                _enqueue_sitemap_batch()
                if not queue:
                    break

            dequeues += 1
            if dequeues > max_dequeues:
                stats["errors"].append(
                    {
                        "url": "",
                        "error": "crawl stopped: dequeue limit exceeded (partial index)",
                    }
                )
                break

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

            final_url = canonical_page_url(normalize_page_url(str(resp.url)))
            if not under_prefix(final_url, origin, path_prefix):
                stats["skipped"] += 1
                continue

            decomposed_soup = _decompose_soup(resp.content)
            main = _find_main_content(decomposed_soup)

            title, body = extract_text_and_title(decomposed_soup, main, str(resp.url))
            print(f"extracted title: {title}")
            return
            entities = extract_structured_entities(main, str(resp.url), body)
            toc = extract_page_toc(main, final_url)

            stats["fetched"] += 1
            fetched_at = int(time.time())
            if on_page:
                await on_page(final_url, title, body, root, fetched_at, entities, toc)

            for link in same_site_links(
                resp.content, str(resp.url), origin, path_prefix
            ):
                try_enqueue(link)
            _enqueue_sitemap_batch()

    return stats
