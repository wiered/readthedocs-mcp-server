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
- `search` local index vs re-crawl every question; optional `source_base` to narrow to one docs tree.
- `list_documentation_pages` when you need the exact URL/title inventory (per root or whole DB).
- `search_in_file` when the page is known but global `search` dedupes to one chunk per URL — several in-file hits (chunks) with line hints.
- `fetch` by hit `id` after `search` / `search_in_file`.
- Cache introspection: roots, page counts, last fetch.

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

## Rules

- Prefer **`fetch(id, start, end)`** over full-page `fetch(id)` on huge pages — token hygiene; **default to a comfortably wide range** around the target lines (document neighborhood), then trim or widen only if needed.
- **`list_documentation_pages`** does not replace `search` for semantic discovery — it lists URLs; pair with `search_in_file` for in-file precision.
- `search` / `search_in_file` before `fetch`; no invented page text if index has it.
- Missing page → re-`index_readthedocs` with correct versioned root (e.g. `…/en/stable/`).
- Stored = HTML-extracted text, not pixel layout.
- Anchors ≠ separate rows; one row per page for `fetch`; FTS ranks overlapping **chunks** for relevance.

## Search -> Fetch rule

- Use `search` (optionally with `source_base`) or `list_documentation_pages` + `search_in_file` to locate the right page and approximate line(s).
- After the first relevant hit, prefer `search_in_file(page_url, …)` to refine within that page before reaching for `fetch`.
- Switch to `fetch(id, start, end)` only when the result needs surrounding context, examples, or nearby prose to answer cleanly.
- Do not chain multiple global `search` calls for the same question if a likely page is already known; use `search_in_file` for page-local refinement.
- **`start`/`end` must include surrounding context** (paragraphs above/below the hit), not the smallest interval that contains only the matched lines — the goal is the local document, not a line-exact excerpt.

## Mandatory search budget

- For one user question, do at most **one global `search` call per docs source** (or one `search` with a fixed `source_base`) before switching to `fetch` or **`search_in_file`**.
- **`search_in_file`** on a known URL does **not** count toward the global `search` reformulation budget — use it for extra in-file chunk hits instead of guessing new global queries.
- If the first `search` returns a relevant hit on the target symbol/method/page, **you must not issue more global `search` calls** for the same question until after at least one `search_in_file` or `fetch`.
- Additional `search` calls are allowed only if:
  - the first `search` returned no relevant hits, or
  - a `fetch`ed page proved to be unrelated.

## Mandatory fetch-first behavior

- If `search` finds a page mentioning the exact API symbol, method name, class name, or quoted phrase from the user’s question, first inspect the page with `search_in_file(page_url, …)` when you need to locate the most relevant chunk.
- Call `fetch(id, start, end)` only after you know which local lines matter, or when the chunk text is too thin to answer cleanly.
- Do not run synonym, variant, or reformulation searches before the first page-local refinement when an exact-match hit already exists.

## Search failure policy

- Before issuing a second `search`, explicitly verify that:
  - no exact-match hit exists, or
  - the fetched exact-match page was insufficient.
- “Wanting more confidence” is **not** a valid reason for repeated `search`.

## Token/latency discipline

- Repeated global `search` reformulations for the same symbol are considered misuse.
- Prefer:
  1. one `search`
  2. one or more `search_in_file` calls on the known page
  3. one `fetch` with a **padded** line range around the hit when full context is needed
  4. one wider `fetch` if the neighborhood was still too tight
  5. only then another global `search` if still blocked

## Bad / Good patterns

Bad:
- `search("send_photo")`
- `search("send_photo file url")`
- `search("photo InputFile")`

Good:
- `search("send_photo")`
- `fetch(id, start, end)` with `start`/`end` bracketing the hit **plus** enough lines before/after for full local meaning
- second `fetch` only if the first window missed neighboring sections
