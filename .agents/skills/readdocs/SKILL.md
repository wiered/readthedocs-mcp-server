---
name: readdocs
description: readdocs-mcp-server — crawl docs root → SQLite FTS5; search/fetch cached text. Use when MCP connected, RTD/Sphinx indexing, readdocs, local doc search/fetch in this repo.
---

# readdocs-mcp-server (this repo)

MCP tools: single docs root URL → crawl HTML → plain text in SQLite + FTS5. Queries hit index, not live HTTP each time.

## Skill path

- **Codex / OpenAI**: `.agents/skills/` (this tree).
- **Cursor**: often `~/.cursor/skills/` or `.cursor/skills/`. If `.agents/skills/` not picked up — add path in Cursor skill settings or copy folder to `.cursor/skills/readdocs/`.

## When

- Index from `http(s)` seed (Sphinx, RTD, static HTML).
- `search` local index vs re-crawl every question.
- `fetch` by hit `id` after `search`.
- Cache introspection: roots, page counts, last fetch.

## Workflow

1. `index_readthedocs` — not indexed or stale.
2. `search` — keyword-style (see below).
3. **`fetch(id, start, end)` preferred** — 1-based inclusive lines; keeps context small. Use `search` hit line + small probe (`fetch` with narrow slice or first slice after tiny range) to pick window; bare `fetch(id)` only when page short or slice cannot answer question.
4. `list_indexed_sources` or resource `readdocs://status` — JSON cache snapshot.

## Tools

| Tool | Notes |
|------|--------|
| `index_readthedocs(seed_url, max_pages=200, request_delay_sec=0.15, replace_source=True)` | Walk under normalized docs root; dedupe URLs (`?highlight=`, `index.html` vs dir); `sitemap.xml` seeds if present; `<a href>`, Sphinx `<link rel="next|prev|…">`. Returns `IndexStats`: `source_base`, `fetched`, `skipped`, `sitemap_seeds`, `errors`, `db_path`. Big sites: raise `max_pages` (cap 5000). |
| `search(query, limit=15)` | Hits: `id`, `title`, `url`, `text` — **one ~line** from matched chunk (real page words, not FTS snippet blob). |
| `fetch(id, start=None, end=None)` | **Default agent habit:** pass `start`/`end` whenever possible — avoids token bloat. Body + `source_base`, `fetched_at`. No slice = whole page. Both `start`/`end` = **1-based inclusive** `splitlines()`, max 5000 lines/call. Response: `total_lines`, clamped `slice_start`/`slice_end`; empty `text` if `start` past EOF. |
| `list_indexed_sources()` | Per `source_base`: `page_count`, `last_fetched_at`. |

**Env (server process):** `READTHEDOCS_MCP_DB`, `READTHEDOCS_MCP_IMPERSONATE` (curl_cffi profile, default `chrome`), transport/host/port for HTTP modes.

## Search (FTS5 lexical, chunked body)

Not vectors. Query → MATCH: **stop words dropped**; **strong** tokens (long, `_`/`.`, digits, mixed case e.g. `InputFile`) → **AND**; short filler with strong → **OR** groups; **fallback** broader OR if strict empty.

**Query tips:**

1. Short concrete strings from docs: API (`send_photo`), types (`InputFile`), modules (`telebot.types`), errors, keys. Long prose = extra rare tokens → over-narrow before fallback.
2. Double quotes = phrase must be consecutive: `"io.IOBase"`, `"send a file"`.
3. Empty/weak hits: synonyms, snake vs CamelCase, module prefix vs bare name, fewer words, higher `limit` (up to 100).
4. `search.text` = one context line; **do not** pull full page by default — `fetch(id, start, end)`; widen slice or follow-up slice using `total_lines` / prior `slice_*`; `fetch(id)` without bounds only for short pages or when slice clearly insufficient.

## Rules

- Prefer **`fetch(id, start, end)`** over full-page `fetch(id)` — token hygiene; expand range only if needed.
- `search` before `fetch`; no invented page text if index has it.
- Missing page → re-`index_readthedocs` with correct versioned root (e.g. `…/en/stable/`).
- Stored = HTML-extracted text, not pixel layout.
- Anchors ≠ separate rows; one row per page for `fetch`; FTS ranks overlapping **chunks** for relevance.
