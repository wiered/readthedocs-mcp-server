"""Small JSON helpers for persisted columns."""

from __future__ import annotations

import json
from typing import Any


def _json_list(raw: str) -> list[Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []
