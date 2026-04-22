"""Environment-driven MCP server binding and transport."""

from __future__ import annotations

import os
from typing import Literal


def mcp_bind_host() -> str:
    return os.environ.get("READTHEDOCS_MCP_HOST", "127.0.0.1")


def mcp_bind_port() -> int:
    return int(os.environ.get("READTHEDOCS_MCP_PORT", "8000"))


def get_mcp_transport() -> Literal["stdio", "sse", "streamable-http"]:
    raw = os.environ.get("READTHEDOCS_MCP_TRANSPORT", "stdio").strip().lower()
    if raw in {"sse", "streamable-http"}:
        return raw
    return "stdio"
