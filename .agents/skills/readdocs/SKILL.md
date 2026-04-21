---
name: readdocs
description: readdocs-mcp-server — crawl docs root → SQLite FTS5; search/fetch cached text. Use when MCP connected, RTD/Sphinx indexing, readdocs, local doc search/fetch in this repo.
---

# readdocs-mcp-server (this repo)

MCP tools: single docs root URL → crawl HTML → plain text in SQLite + FTS5. Queries hit index, not live HTTP each time.

## When

- Index from `http(s)` seed (Sphinx, RTD, static HTML).
- `search` local index vs re-crawl every question; optional `source_base` to narrow to one docs tree.
- `list_documentation_pages` when you need the exact URL/title inventory (per root or whole DB).
- `search_in_file` when the page is known but global `search` dedupes to one chunk per URL — several in-file hits (chunks) with line hints.
- `fetch` by hit `id` after `search` / `search_in_file`.
- Cache introspection: roots, page counts, last fetch.

## Quick routing

- If you already know the exact `page_url`:
  - use `search_in_file(page_url, query)`
  - then `fetch(id, start, end)` if you need surrounding context

- If you know the docs root / `source_base`, but not the page:
  - run one scoped `search(query, source_base=...)`
  - then refine with `search_in_file(page_url, query)`
  - then `fetch(...)` if needed

- If you are not sure whether the docs root is already indexed:
  - check `list_indexed_sources()`
  - if missing or stale, run `index_readthedocs(...)`

- If you need the page inventory, exact path, or URL discovery:
  - use `list_documentation_pages(...)`
  - then `search_in_file(...)` on the chosen page

## Workflow

1. `index_readthedocs` — not indexed or stale.
2. Orient: `list_indexed_sources` / `readdocs://status` for `source_base` values; **`list_documentation_pages(source_base=…)`** to see concrete URLs/titles when search feels noisy or you need the right file path.
3. **`search(query, source_base=…)`** — scoped keyword search (still **one hit per URL**); use when the question clearly belongs to one indexed root and the target page is not already known.
4. **`search_in_file(page_url, query)`** — when the HTML page is known (from `list_documentation_pages` or a prior hit): **multiple results per file** (one per matching FTS chunk). Prefer this over repeated global searches when refining a known page.
5. **`fetch(id, start, end)`** — 1-based inclusive lines. Use it only when you need surrounding context around a known hit, code examples, or neighboring bullets/headings. Request a **padded window** around the target lines rather than a razor-thin band.
6. `list_indexed_sources` or resource `readdocs://status` — JSON cache snapshot.

## Tools

| Tool | Notes |
|------|--------|
| `index_readthedocs(seed_url, max_pages=200, request_delay_sec=0.15, replace_source=True)` | Walk under normalized docs root; dedupe URLs (`?highlight=`, `index.html` vs dir); `sitemap.xml` seeds if present; `<a href>`, Sphinx `<link rel="next|prev|…">`. Returns `IndexStats`: `source_base`, `fetched`, `skipped`, `sitemap_seeds`, `errors`, `db_path`. Big sites: raise `max_pages` (cap 5000). |
| `search(query, limit=15, source_base=None)` | Global or **scoped to one `source_base`** (same host/path root as indexing). At most **one hit per page** (best chunk). Each hit: `text` ≈ one context line; `line` ≈ page line; `chunk_line_start` / `chunk_line_end` = chunk span in the full page (1-based, inclusive). |
| `search_in_file(page_url, query, limit=15)` | **Only** that indexed `page_url` (absolute http(s)). **Several hits allowed** from the same file (different chunks). Same fields as `search` hits; use chunk line span + `line` to drive `fetch` slices. |
| `list_documentation_pages(source_base=None, url_contains=None, limit=200, offset=0)` | Paginated **inventory**: `url`, `title`, `source_base` per row; `total` for filters. Filter by docs root and/or substring in URL (path segment). `limit` 1–500. |
| `fetch(id, start=None, end=None)` | Pass `start`/`end` for large pages — avoids pulling the whole file. **Slice = context window:** bracket the interesting lines with enough lines before/after (section-level context), not the minimal single-line interval. Body + `source_base`, `fetched_at`. No slice = whole page. Both `start`/`end` = **1-based inclusive** `splitlines()`, max 5000 lines/call. Response: `total_lines`, clamped `slice_start`/`slice_end`; empty `text` if `start` past EOF. |
| `list_indexed_sources()` | Per `source_base`: `page_count`, `last_fetched_at`. |

**Env (server process):** `READTHEDOCS_MCP_DB`, `READTHEDOCS_MCP_IMPERSONATE` (curl_cffi profile, default `chrome`), transport/host/port for HTTP modes.

## Search (FTS5 lexical, chunked body)

Not vectors. Query → MATCH: **stop words dropped**; **strong** tokens (long, `_`/`.`, digits, mixed case e.g. `InputFile`) → **AND**; short filler with strong → **OR** groups; **fallback** broader OR if strict empty.

**Query tips:**

1. Short concrete strings from docs: API (`send_photo`), types (`InputFile`), modules (`telebot.types`), errors, keys. Long prose = extra rare tokens → over-narrow before fallback.
2. Double quotes = phrase must be consecutive: `"io.IOBase"`, `"send a file"`.
3. Empty/weak hits: synonyms, snake vs CamelCase, module prefix vs bare name, fewer words, higher `limit` (up to 100).
4. **Wrong root / noisy hits:** pass `source_base` from `list_indexed_sources` so `search` only scans that tree.
5. **Known file, many mentions:** use `search_in_file(page_url, …)` instead of repeated global `search`; then `fetch` around each `chunk_line_*` / `line` with padding.
6. `search.text` = one context line; **do not** pull full page by default — `fetch(id, start, end)` with **generous padding** around the hit line; if the first slice still lacks surrounding explanation, widen once using `total_lines` / prior `slice_*`; `fetch(id)` without bounds only for short pages or when a bounded slice clearly insufficient.

## Retrieval discipline

- Before retrieving more, estimate how much depth the user’s question actually requires, and stop once you have enough to answer accurately.
- Do not keep searching or fetching only to increase confidence when the answer is already clear.
- Prefer the minimum retrieval needed for the question type: conceptual questions need less evidence than exhaustive or edge-case questions.
- Avoid repeated near-duplicate searches or adjacent fetches unless they are likely to add genuinely new information.
- Use indexed text before answering: no invented page content when `search`, `search_in_file`, or `fetch` can retrieve it.
- Locate first, then fetch: use one scoped `search` or `list_documentation_pages` + `search_in_file` to identify the page and approximate lines.
- For one user question, do at most **one global `search` per docs source** before switching to page-local work.
- If a hit mentions the exact API symbol, method, class, page, or quoted phrase, do not reformulate globally yet; refine with `search_in_file(page_url, query)` and then `fetch` if needed.
- `search_in_file` does not count against the global search budget; use it for extra chunks on a known page instead of repeated global searches.
- Call `fetch(id, start, end)` only when you need surrounding prose, examples, or neighboring bullets/headings; prefer bounded fetches over full-page fetches on large pages.
- `start`/`end` must be a padded document neighborhood around the hit, not a razor-thin match span. If still too tight, widen once using `total_lines` / prior `slice_*`.
- A second global `search` is allowed only after verifying that no exact-match hit exists, or that `search_in_file` / `fetch` proved the exact-match page insufficient or unrelated. More confidence alone is not enough.
- If a page is missing, stale, or from the wrong version/root, run `index_readthedocs(...)` with the correct versioned seed (for example `.../en/stable/`).
- `list_documentation_pages` is URL/title inventory, not semantic search; pair it with `search_in_file` after choosing a page.
- Stored content is HTML-extracted text, not pixel layout. Anchors are not separate rows: `fetch` returns one row per page, while FTS ranks overlapping chunks.
- Token/latency default: one `search` -> one or more `search_in_file` calls -> one padded `fetch` -> one wider `fetch` if needed -> another global `search` only if still blocked.
- For simple conceptual questions (e.g. "difference between X and Y"):
  - You MUST NOT exceed:
    - 1 global `search`
    - 1–2 `search_in_file`
    - 1–2 `fetch`
  - Exceeding this budget requires clear evidence that the initial results are insufficient.
- If you already understand the difference and usage after initial retrieval (even if some details are missing):
  - STOP and answer.
  - Do not perform additional searches or fetches for confirmation.

## Bad / Good patterns

Bad:
- `search("send_photo")`
- `search("send_photo file url")`
- `search("photo InputFile")`

Good:
- `search("send_photo")`
- `fetch(id, start, end)` with `start`/`end` bracketing the hit **plus** enough lines before/after for full local meaning
- second `fetch` only if the first window missed neighboring sections
