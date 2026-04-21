# readdocs-mcp-server

MCP server for indexing and searching Read the Docs / Sphinx documentation locally.

The server crawls a documentation tree, stores page text in SQLite with FTS5, and exposes MCP tools compatible with the common `search` / `fetch` workflow used by Codex, Cursor, and other MCP clients.

## Features

- Index Read the Docs and other Sphinx HTML sites into a local SQLite database
- Search indexed pages with FTS5
- Search Sphinx Python classes and methods with structured params, notes, warnings, and version markers
- Fetch full page text by URL id
- Expose MCP prompts for common slash-command workflows
- Support `stdio`, `sse`, and `streamable-http` transports
- Use `curl_cffi` browser impersonation to handle sites behind Cloudflare more reliably

## Requirements

- Python 3.10+
- Windows, macOS, or Linux

## Install

### With `uv` from source

```bash
uv venv
uv pip install -e .
```

### With `pip` from source

```bash
python -m venv .venv
.venv\Scripts\pip install -e .
```

After installation, the console entrypoint is:

```bash
readdocs-mcp
```

You can also run the module directly:

```bash
python -m readdocsserver
```

## Local development

Run the MCP inspector:

```bash
uv run mcp dev src/readdocsserver/main.py
```

Run the server over stdio:

```bash
uv run python -m readdocsserver
```

Lint:

```bash
.\.venv\Scripts\ruff check .
```

If the local Ruff binary is not available:

```bash
ruff check .
```

## MCP tools

- `index_readthedocs`: crawl and index a docs tree
- `search`: full-text search over indexed pages
- `search_entities`: search structured Sphinx classes and methods
- `lookup_symbol`: find one structured symbol and return its page, anchor, line range, and short context
- `get_entity`: fetch one structured class or method by `entity_id`
- `list_class_methods`: list methods for a structured class
- `get_entity_context`: search one entity and return its params and notes/warnings context
- `fetch`: fetch a page by canonical URL
- `list_indexed_sources`: list indexed documentation roots

## MCP prompts

Compatible MCP clients can expose these as slash commands:

- `index-docs`
- `search-docs`
- `read-page`

## Environment variables

- `READTHEDOCS_MCP_DB`: path to the SQLite database
- `READTHEDOCS_MCP_IMPERSONATE`: `curl_cffi` browser profile, default `chrome`
- `READTHEDOCS_MCP_TRANSPORT`: `stdio`, `sse`, or `streamable-http`
- `READTHEDOCS_MCP_HOST`: bind host for HTTP transports, default `127.0.0.1`
- `READTHEDOCS_MCP_PORT`: bind port for HTTP transports, default `8000`

## Install in Codex

If Codex supports command-based MCP servers, point it at the installed entrypoint or Python module.

Example using the console script:

```json
{
  "mcpServers": {
    "readdocs": {
      "command": "readdocs-mcp"
    }
  }
}
```

Example using the virtualenv Python explicitly:

```json
{
  "mcpServers": {
    "readdocs": {
      "command": "G:\\programming\\py\\git\\readthedocs-mcp-server\\.venv\\Scripts\\python.exe",
      "args": ["-m", "readdocsserver"]
    }
  }
}
```

If you want a custom database path:

```json
{
  "mcpServers": {
    "readdocs": {
      "command": "readdocs-mcp",
      "env": {
        "READTHEDOCS_MCP_DB": "G:\\programming\\py\\readdocs-index.sqlite"
      }
    }
  }
}
```

## Install in Cursor

Cursor MCP configuration is also command-based. Use either the installed script or the virtualenv Python.

Example:

```json
{
  "mcpServers": {
    "readdocs": {
      "command": "G:\\programming\\py\\git\\readthedocs-mcp-server\\.venv\\Scripts\\python.exe",
      "args": ["-m", "readdocsserver"]
    }
  }
}
```

If Cursor is launched outside the project environment, using the full Python path is usually more reliable than relying on `PATH`.

## Example workflow

1. Call `index_readthedocs` with a docs URL such as `https://docs.readthedocs.io/en/stable/`.
2. Call `search` with keywords.
3. Call `fetch` with the `id` returned by `search`.

For Sphinx Python API pages, re-index a source and use `search_entities` for
queries such as `LayoutView`, then `get_entity` or `list_class_methods` for
structured class/method details. Existing databases get the new schema
automatically, but structured entities are populated only when a page is crawled
again.

If you already know an API symbol such as `discord.ui.LayoutView`, use
`lookup_symbol(source_base, symbol_name)` first. It returns the page URL, anchor,
line range, and a compact context window in one call. Use `get_entity` afterward
only when you need full structured params, notes, or child methods.

## Notes

- This server stores indexed content locally in SQLite.
- The crawler stays under the documentation root derived from the seed URL.
- Some sites may still block automated traffic even with browser impersonation.
