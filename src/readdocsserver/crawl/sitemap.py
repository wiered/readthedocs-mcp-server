"""Sitemap discovery for crawl seed URLs."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

from curl_cffi.requests import AsyncSession

from readdocsserver.crawl.urls import (
    canonical_page_url,
    normalize_page_url,
    under_prefix,
)

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
