---
name: readdocs
description: Guides agents through the readdocs-mcp-server workflow—indexing Sphinx or Read the Docs HTML into a local SQLite FTS index, searching it, and fetching full page text. Use when the user connects this MCP server, mentions indexing RTD/Sphinx docs, readdocs, or local documentation search/fetch tools in this repository.
---

# Read the Docs MCP (this repo)

The **readdocs-mcp-server** package exposes MCP tools that crawl documentation under a single docs root URL, store plain text in SQLite + FTS5, and answer queries without live HTTP on each question.

## Where this skill lives (Codex vs Cursor)

- **Codex / OpenAI agents**: project skills often live under `.agents/skills/`; this file is already there.
- **Cursor**: built-in discovery commonly uses `~/.cursor/skills/` or `.cursor/skills/` in the repo. If Cursor does not pick up `.agents/skills/` automatically, add that path in Cursor skill settings or copy this folder to `.cursor/skills/readdocs/`.

## When to use

- Index a documentation site from an `http(s)` seed URL (Sphinx, Read the Docs, or similar static HTML trees).
- Search the local index instead of re-crawling for every question.
- Fetch one page by URL after `search` returns an `id`.
- Inspect what is already cached (roots, page counts, last fetch).

## Workflow

1. Run `index_readthedocs` when the site is not indexed or the index is stale.
2. Run `search` with a natural-language or keyword query.
3. Run `fetch` with the hit `id` (same as the page URL) for full text.
4. Use `list_indexed_sources` or the `readdocs://status` resource for a JSON snapshot of the cache.

## Tool semantics

- `index_readthedocs(seed_url, max_pages=200, request_delay_sec=0.15, replace_source=True)` walks HTML under the normalized docs root, deduplicates URLs (including `?highlight=` and `index.html` vs directory URLs), seeds the queue from `sitemap.xml` when present, follows `<a href>` and common Sphinx `<link rel="next|prev|…">` navigation, then stores each page. Returns `IndexStats` with `source_base`, `fetched`, `skipped`, `sitemap_seeds`, `errors`, and `db_path`. For very large sites, raise `max_pages` (allowed up to 5000).
- `search(query, limit=15)` returns ranked hits with `id`, `title`, `text` (snippet), and `url`; use `id` with `fetch`.
- `fetch(id)` returns full stored body text plus metadata (`source_base`, `fetched_at`), or a not-found payload if the URL was never indexed.
- `list_indexed_sources()` lists each `source_base`, `page_count`, and `last_fetched_at`.

Optional environment (server process): `READTHEDOCS_MCP_DB` (SQLite path), `READTHEDOCS_MCP_IMPERSONATE` (curl_cffi TLS profile, default `chrome`), transport/host/port variables for HTTP modes.

## Practical rules

- Prefer `search` before `fetch`; do not invent page contents when the index can supply them.
- If a page is missing, re-run `index_readthedocs` with the correct versioned root URL (for example `…/en/stable/`).
- Treat stored content as extracted text from HTML, not a layout-perfect copy of the site.
- Section-level anchors are not separate index rows; the whole page body is one FTS document unless the product changes.
