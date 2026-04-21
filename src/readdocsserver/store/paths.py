"""Default on-disk location for the SQLite index."""

from __future__ import annotations

from pathlib import Path


def default_db_path() -> Path:
    return Path.home() / ".cache" / "readdocs-mcp" / "index.sqlite"
