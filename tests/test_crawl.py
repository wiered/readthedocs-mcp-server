"""Tests for readdocsserver.crawl URL helpers and HTML extraction."""

from __future__ import annotations

import pytest

from readdocsserver.crawl import (
    _parse_sitemap_urls,
    canonical_page_url,
    extract_text_and_title,
    normalize_doc_root,
    same_site_links,
    under_prefix,
)


@pytest.mark.parametrize(
    ("url", "expected_prefix"),
    [
        ("https://rtd.io/en/stable/index.html", "https://rtd.io/en/stable/"),
        ("https://rtd.io/en/stable/", "https://rtd.io/en/stable/"),
        ("https://rtd.io/en/stable", "https://rtd.io/en/stable/"),
    ],
)
def test_normalize_doc_root(url: str, expected_prefix: str) -> None:
    assert normalize_doc_root(url) == expected_prefix


def test_canonical_strips_highlight() -> None:
    u = "https://x.com/doc/page.html?highlight=foo&other=1"
    c = canonical_page_url(u)
    assert "highlight" not in c
    assert "other=1" in c


def test_canonical_index_html() -> None:
    c = canonical_page_url("https://x.com/en/index.html")
    assert c.endswith("/en/")
    assert "index.html" not in c


def test_under_prefix() -> None:
    origin = "https://docs.python.org"
    prefix = "/3/"
    assert under_prefix("https://docs.python.org/3/library/os.html", origin, prefix) is True
    assert under_prefix("https://docs.python.org/2/", origin, prefix) is False
    assert under_prefix("https://evil.com/3/", origin, prefix) is False


def test_parse_sitemap_urlset() -> None:
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://ex.com/a</loc></url>
  <url><loc>https://ex.com/b</loc></url>
</urlset>"""
    pages, nested = _parse_sitemap_urls(xml)
    assert pages == ["https://ex.com/a", "https://ex.com/b"]
    assert nested == []


def test_parse_sitemap_index() -> None:
    xml = b"""<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://ex.com/sm1.xml</loc></sitemap>
</sitemapindex>"""
    pages, nested = _parse_sitemap_urls(xml)
    assert pages == []
    assert nested == ["https://ex.com/sm1.xml"]


def test_extract_text_rst_content() -> None:
    html = b"""<!doctype html><html><head><title>API</title></head>
<body><div class="rst-content"><p>Hello</p><script>removed()</script></div></body></html>"""
    title, text = extract_text_and_title(html, "https://x/")
    assert title == "API"
    assert "Hello" in text
    assert "removed" not in text


def test_same_site_links_filters_prefix() -> None:
    html = b"""
    <html><body>
    <a href="/en/one/">one</a>
    <a href="https://docs.example.org/en/two/">two</a>
    <a href="https://other.com/x">bad</a>
    </body></html>
    """
    base = "https://docs.example.org/en/index.html"
    origin = "https://docs.example.org"
    prefix = "/en/"
    links = same_site_links(html, base, origin, prefix)
    assert any("one" in u or "two" in u for u in links)
    assert all("other.com" not in u for u in links)
