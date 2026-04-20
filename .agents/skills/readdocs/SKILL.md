---
name: readdocs
description: Use this skill when working with the Read the Docs MCP server in this repository: index a documentation site, search the local FTS index, fetch indexed pages, or inspect index status.
---

# Read the Docs MCP

Use the local Read the Docs MCP server to turn Sphinx/Read the Docs HTML into a searchable SQLite index.

## When to use

- Index a documentation site from an `http(s)` seed URL.
- Search already indexed docs instead of re-crawling the site.
- Fetch one page by its canonical URL after search.
- Check which documentation roots are already stored.

## Workflow

1. Start with `index_readthedocs` when the target site is not yet indexed.
2. Use `search` for natural-language lookup against the local FTS index.
3. Use `fetch` on the returned page `id` to read the full page text.
4. Use `list_indexed_sources` or `readdocs://status` to inspect the current cache.

## Tool semantics

- `index_readthedocs(seed_url, max_pages=200, request_delay_sec=0.15, replace_source=True)` crawls HTML under the docs root and stores each page in SQLite.
- `search(query, limit=15)` returns ranked hits with `id`, `title`, `text`, and `url`; treat `id` as the page URL for `fetch`.
- `fetch(id)` returns the full stored page text and metadata for that URL.
- `list_indexed_sources()` returns the cached source roots, page counts, and last fetch times.

## Practical rules

- Prefer `search` before `fetch`; do not guess page contents when the index can answer.
- If a page is missing, re-run `index_readthedocs` on the correct seed URL.
- Use the returned page text as source material for answers, code changes, and API details.
- Treat the indexed content as a text extraction of HTML, not a perfect mirror of the site.
