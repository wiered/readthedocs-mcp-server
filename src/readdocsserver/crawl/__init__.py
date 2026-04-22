"""Crawl Read the Docs / Sphinx HTML under a documentation root URL."""

from __future__ import annotations

from readdocsserver.crawl.entities import extract_structured_entities
from readdocsserver.crawl.links import same_site_links
from readdocsserver.crawl.render import extract_text_and_title
from readdocsserver.crawl.runner import crawl_readthedocs
from readdocsserver.crawl.sitemap import _parse_sitemap_urls, discover_sitemap_seed_urls
from readdocsserver.crawl.toc import extract_page_toc
from readdocsserver.crawl.urls import (
    canonical_page_url,
    normalize_doc_root,
    normalize_page_url,
    under_prefix,
)

__all__ = [
    "_parse_sitemap_urls",
    "canonical_page_url",
    "crawl_readthedocs",
    "discover_sitemap_seed_urls",
    "extract_page_toc",
    "extract_structured_entities",
    "extract_text_and_title",
    "normalize_doc_root",
    "normalize_page_url",
    "same_site_links",
    "under_prefix",
]
