"""Compose entity body text indexed in doc_entities_fts."""

from __future__ import annotations

import sqlite3
from typing import Any


def _note_value(note: Any, key: str) -> str:
    if isinstance(note, sqlite3.Row):
        return str(note[key] or "")
    if isinstance(note, dict):
        return str(note.get(key) or "")
    return ""


def _entity_notes_fts_text(notes: list[Any]) -> str:
    lines: list[str] = []
    for note in notes:
        kind = _note_value(note, "kind")
        version = _note_value(note, "version")
        text = _note_value(note, "text")
        if not kind and not version and not text:
            continue
        human_label = ""
        if kind == "versionadded":
            human_label = "Added in version"
        elif kind == "versionchanged":
            human_label = "Changed in version"
        elif kind == "versionremoved":
            human_label = "Removed in version"
        elif kind == "deprecated":
            human_label = "Deprecated since version"
        parts = [kind, human_label, version, text]
        lines.append(" ".join(part for part in parts if part).strip())
    return "\n".join(lines)


def _entity_fts_body_text(body_text: str, notes: list[Any]) -> str:
    notes_text = _entity_notes_fts_text(notes)
    return "\n\n".join(part for part in (body_text, notes_text) if part)
