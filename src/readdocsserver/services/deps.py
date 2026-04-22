"""Dependency helpers for MCP tool handlers."""

from __future__ import annotations

import os
from pathlib import Path

from readdocsserver.store import DocIndex


def get_doc_index() -> DocIndex:
    path = os.environ.get("READTHEDOCS_MCP_DB")
    return DocIndex(Path(path) if path else None)
