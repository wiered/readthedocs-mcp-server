---
name: readdocs
description: >
  Use this skill whenever the readthedocs-docs / readdocs MCP server is connected, or the user asks to search, fetch, index, or reason about Read the Docs / Sphinx documentation from the local SQLite index. Trigger on RTD URLs, doc lookup, API reference questions, “what does the docs say”, symbol navigation, indexing a doc tree, FTS search, fetch slices, entity/graph queries—even if the user does not name “MCP” or “readdocs”. Follow this workflow instead of guessing page contents.
---

# readdocs MCP — effective use

MCP server name: **`readthedocs-docs`**. Indexed HTML becomes plain text in SQLite; search uses FTS5 chunks (overlapping). **`fetch` returns one line per logical line of that stored text** (not live HTML layout). Anchors are not separate rows.

Resource: `readdocs://status` — same idea as `list_indexed_sources` (JSON snapshot).

---

## When

- User wants **facts from documentation** already in the index (or must be indexed first).
- Tasks: find a page, compare concepts, explain API, trace **classes / methods / symbols**, explore **related symbols**, read a **section with context**.
- You have (or can get) a **`source_base`** when multiple doc roots exist.
- **Do not** use this skill for pure speculation: if the answer must come from docs, **retrieve** then cite; do not invent page text.

---

## Routing — what to do first

| Situation | First step |
|-----------|------------|
| **Unknown** whether anything is indexed | `list_indexed_sources` or read `readdocs://status` |
| Indexed, **unknown** which doc site | `list_indexed_sources` → pick `source_base` |
| Wrong / missing / stale version | `index_readthedocs` with correct **`seed_url`** (e.g. `.../en/stable/`), set `replace_source` if re-crawl needed |
| Know **`source_base`**, need **page list / TOC**, not semantics | `list_documentation_pages` (optionally `url_contains`, pagination `offset`) |
| Know **`source_base`**, **broad topic** or prose | One **`search`** (with `source_base`) |
| **Page URL known**, need more hits on that page | **`search_in_file`** — never spend a second global `search` for this |
| **API name** known (`Module.Class.method`) | **`lookup_symbol`** or **`search_entities`** / **`get_entity_context`** |
| Have **`entity_id`** from `search_entities` | **`get_entity`** |
| Need **methods of a class** | **`list_class_methods`** (`class_entity_id` or `class_name` + optional `source_base`) |
| Need **graph neighbors** (bases, params, see-also, …) | **`related_symbols`** |
| Index / graph quality questions | **`get_symbol_graph_stats`** |

---

## Workflow

1. **Inventory** — `list_indexed_sources` (or status resource). Note `source_base` values.
2. **Locate** — exactly **one** global **`search`** *or* **`search_entities`** / **`lookup_symbol`** / **`get_entity_context`** for the question *until you are working inside a known page or symbol*. Prefer **`search_in_file`** on a known `page_url` over a second global `search`.
3. **Narrow** — `search_in_file(page_url, …)` for multiple chunk hits on the same URL. `search` returns **at most one hit per page**; it cannot replace `search_in_file` for “all mentions on this page”.
4. **Read** — `fetch(id=page_url, start=…, end=…)` with **both** `start` and `end` or **neither** (full page). Pad around the hit; avoid whole huge pages without need.
5. **Graph / entities** — `get_entity`, `related_symbols`, `list_class_methods` when the question is structural, not free text.
6. **Stop** — when evidence answers the question; do not loop “for confidence”.

**Global `search` budget (discipline below):** default **≤ 1** per user question per doc source until localized; **second** global `search` only if verified dead end after `search_in_file` / `fetch` on the chosen page.

---

## Usage

### Indexing

- **`index_readthedocs`**: `seed_url` (absolute doc entry), `max_pages` (1–5000, default 200), `request_delay_sec` (0–30), `replace_source` (if true, clears that normalized root first). Returns stats and DB path.

### Table of contents

- **`list_documentation_pages`**: inventory of URLs, titles, and **per-page section TOC**. Not semantic search — after picking URLs, use **`search_in_file`** or **`fetch`**.

### Global vs local text search

- **`search`**: FTS over chunks; optional `source_base`; `limit` 1–100 (default 15). **One best chunk per URL** in results. Use `chunk_line_start` / `chunk_line_end` and approximate `line` to plan `fetch` windows.
- **`search_in_file`**: same FTS engine, **one `page_url`**, **multiple** hits from same page allowed. Use after you know the page.

### Point symbol resolution

- **`lookup_symbol`**: requires `source_base` + `symbol_name` (as in docs). Returns page URL, line ranges, short **context** slice. For exact reading, still use **`fetch`** with `start`/`end` when needed.

### Entities

- **`search_entities`**: FTS on structured entities; optional `kind` (`class` / `method`), `source_base`, `limit`, `include_related`.
- **`get_entity`**: `entity_id` from search; toggles `include_methods`, `include_params`, `include_notes`.
- **`get_entity_context`**: “question → one entity” shortcut; optional `kind`, `source_base`, `include_notes`.
- **`list_class_methods`**: pass **`class_entity_id`** or **`class_name`** (+ optional `source_base`).

### Graph

- **`related_symbols`**: `source_base`, `symbol_name`, optional `edge_types`, `direction` (`out` | `in` | `both`), `limit`.
- Edge type examples: `has_method`, `inherits_from`, `returns`, `accepts_parameter_type`, `references`, `see_also`, `mentioned_in_note`, `mentioned_in_warning`, `only_valid_in`, `requires`, `use_instead`, `similar_to`, `converts_to`.

### `fetch` boundaries

- **`id`**: canonical page URL (same as `url` / `id` from search results).
- **`start` / `end`**: **1-based, inclusive**, **both required together** or omit both. Max slice **5000** lines (`end - start + 1 ≤ 5000`).
- **Padding**: start from `chunk_line_start`–`chunk_line_end`, expand ±50–200 lines (or once more using `total_lines`) for headings / examples. If `start` beyond file length, text may be empty but metadata still returned.
- **`total_lines`**: full page line count even when sliced; use for sane expansion.

### Query formulation

- Prefer **exact API strings** (`module.Class`, `snake_case`) in `search` / `search_entities`.
- Natural-language queries OK; engine relaxes match if needed.
- If **empty results**: shorten to 1–2 tokens or switch to known page + **`search_in_file`**.
- Always pass **`source_base`** when multiple roots exist.

### MCP prompts (optional templates)

- **`index-docs`** → instructs `index_readthedocs` with `seed_url`.
- **`search-docs`** → `search` (+ `source_base` from sources if needed).
- **`read-page`** → `fetch` for `id`, optional user question.

---

## Tools — parameters (concise)

| Tool | Parameters |
|------|------------|
| `index_readthedocs` | `seed_url`, `max_pages`, `request_delay_sec`, `replace_source` |
| `list_indexed_sources` | _(none)_ |
| `search` | `query`, `limit`, `source_base?` |
| `search_in_file` | `page_url`, `query`, `limit` |
| `search_entities` | `query`, `kind?`, `source_base?`, `limit`, `include_related?` |
| `get_entity` | `entity_id`, `include_methods?`, `include_params?`, `include_notes?` |
| `get_entity_context` | `query`, `kind?`, `include_notes?`, `source_base?` |
| `list_class_methods` | `class_name?`, `class_entity_id?`, `source_base?` |
| `lookup_symbol` | `source_base`, `symbol_name`, `include_related?` |
| `related_symbols` | `source_base`, `symbol_name`, `edge_types?`, `direction?`, `limit?` |
| `get_symbol_graph_stats` | `source_base` |
| `list_documentation_pages` | `source_base?`, `url_contains?`, `limit?`, `offset?` |
| `fetch` | `id`, `start?` + `end?` (together or omit both) |

---

## Discipline

- **Judge depth** — enough evidence to answer; then **stop**. No extra global search “to feel sure”.
- **Minimum retrieval** — conceptual questions need less than edge-case or exhaustive audits. No duplicate near-identical queries or redundant adjacent fetches.
- **Do not invent** — page body must come from **`search` / `search_in_file` / `fetch` / entity tools**, not from memory.
- **Localize, then pull** — one narrow `search` *or* `list_documentation_pages` + `search_in_file` before large `fetch`.
- **Global `search` budget** — **at most one** global `search` per question per doc root **until** you work a specific page. **`search_in_file` does not consume** this budget; use it on a **known** `page_url` instead of repeating global search.
- **Exact hit** — if hit already names symbol / page / quote: do **not** rephrase into another global query; narrow with **`search_in_file`**, then **`fetch`**.
- **`fetch` with intent** — only when prose, examples, or neighboring headings matter. Large pages → **bounded** slices, not full page unless necessary. Pad around hit; if thin, **one** controlled widen using `total_lines` / prior slice.
- **Second global `search`** — only if **verified**: no precise hit, or page from `search_in_file` / `fetch` is **wrong or insufficient**. “Mild uncertainty” alone is **not** enough.
- **Index / version** — missing or wrong branch → **`index_readthedocs`** with correct `seed_url`. **`list_documentation_pages`** = URL/title/TOC inventory, not meaning.
- **Data model** — stored text from HTML; anchors not separate lines; FTS chunks overlap; `fetch` is line-oriented plain text.
- **Default pipeline** — one `search` → one or more `search_in_file` → one padded `fetch` → optional one wider `fetch` → second global `search` **only if still stuck**.
- **Simple “X vs Y”** — hard cap: **1** global `search`, **1–2** `search_in_file`, **1–2** `fetch`; more only with **explicit** reason (too few hits). If difference already clear → **answer immediately**, no confirmatory extra calls.

---

## Bad / Good patterns

**Bad**

- Chaining **two global `search`** calls with paraphrased queries before locking a page.
- Using **`search`** hoping for multiple snippets from the **same** URL (use **`search_in_file`**).
- **`fetch`** entire 20k-line page when a 120-line window around `chunk_line_*` suffices.
- Answering API details from training data when the project expects **indexed** docs.
- **`list_documentation_pages`** keyword-guessing instead of **`search_in_file`** after picking a URL.
- Ignoring **`source_base`** when multiple indexes exist → noisy or wrong project.

**Good**

- `list_indexed_sources` → `search(..., source_base=…)` → pick URL → `search_in_file` → `fetch` with padded `chunk_line_start`–`chunk_line_end`.
- Known symbol: `lookup_symbol` → optional `related_symbols` → **`fetch`** only on gaps.
- Known class name: `search_entities` / `get_entity_context` → `get_entity` / `list_class_methods`.
- Empty `search`: shorten query or jump to TOC URL from `list_documentation_pages`, then `search_in_file`.
- Stop after one bounded `fetch` if the manual section fully answers the question.

---

## Mental model

| Need | Tool |
|------|------|
| “Where in the docs is this discussed?” | `search` |
| “I am on this URL; find all mentions.” | `search_in_file` |
| “Which class / method is this?” | `search_entities`, `lookup_symbol`, `get_entity_context` |
| “What else connects?” | `related_symbols` |
| “Read this part of the page.” | `fetch` |

If unsure whether indexed: **`list_indexed_sources`** first.
