"""FastMCP server instructions string."""

INSTRUCTIONS = """
Index Read the Docs / Sphinx HTML locally and query it with MCP search/fetch tools.

Available MCP capabilities:
- Tools for indexing, searching, fetching, listing indexed sources, listing pages per root, and scoped search (whole source or single page).
- Symbol lookup for structured Sphinx/Python classes and methods when entity data is available.
- Related symbol lookup for typed graph neighbors such as class methods, return types, parameter types, and base classes.
- Prompts exposed as slash commands in compatible MCP clients for common workflows.
- A status resource with the current database path and indexed source summary.

Environment:
- READTHEDOCS_MCP_DB: optional SQLite index path (default: ~/.cache/readdocs-mcp/index.sqlite).
- READTHEDOCS_MCP_IMPERSONATE: optional curl_cffi browser TLS profile (default: chrome).
- READTHEDOCS_MCP_TRANSPORT: stdio (default), sse, or streamable-http.
- READTHEDOCS_MCP_HOST / READTHEDOCS_MCP_PORT: bind address for HTTP transports.
""".strip()
