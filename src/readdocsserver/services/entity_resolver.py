"""Resolve documented entity names from type annotations."""

from __future__ import annotations

import re
import sqlite3

from readdocsserver.schemas.domain import SymbolInfo, SymbolMaps

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


class SymbolMapsRuntime:
    """
    Incremental qualname/name maps for edge building while indexing.

    ``build_symbol_maps`` reloads every entity for a source on each page; that is
    O(pages * entities) and stalls large doc crawls. This structure is updated per
    page in O(entities on page).
    """

    __slots__ = (
        "by_qualname",
        "symbols",
        "_entity_qualname",
        "_entity_name",
        "_name_to_entities",
    )

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.by_qualname: dict[str, str] = {}
        self.symbols: dict[str, SymbolInfo] = {}
        self._entity_qualname: dict[str, str] = {}
        self._entity_name: dict[str, str] = {}
        self._name_to_entities: dict[str, set[str]] = {}

    def seed_from_db(self, conn: sqlite3.Connection, source_base: str) -> None:
        self.reset()
        rows = conn.execute(
            """
            SELECT entity_id, name, qualname
            FROM doc_entities
            WHERE source_base = ?
            """,
            (source_base,),
        ).fetchall()
        for row in rows:
            self._add(
                str(row["entity_id"]),
                str(row["name"] or ""),
                str(row["qualname"] or ""),
            )

    def remove_entities_for_page(
        self, conn: sqlite3.Connection, source_base: str, page_url: str
    ) -> None:
        rows = conn.execute(
            """
            SELECT entity_id FROM doc_entities
            WHERE source_base = ? AND page_url = ?
            """,
            (source_base, page_url),
        ).fetchall()
        for row in rows:
            self._remove(str(row["entity_id"]))

    def add_entity_rows(self, rows: list[tuple[str, str, str]]) -> None:
        for entity_id, name, qualname in rows:
            self._add(entity_id, name, qualname)

    def to_symbol_maps(self) -> SymbolMaps:
        by_unique_name: dict[str, str] = {}
        for name, eids in self._name_to_entities.items():
            if len(eids) == 1:
                by_unique_name[name] = next(iter(eids))
        return SymbolMaps(
            by_qualname=dict(self.by_qualname),
            by_unique_name=by_unique_name,
            symbols=dict(self.symbols),
        )

    def _remove(self, entity_id: str) -> None:
        qualname = self._entity_qualname.pop(entity_id, None)
        if qualname is not None:
            self.by_qualname.pop(qualname, None)
            self.symbols.pop(qualname, None)
        name = self._entity_name.pop(entity_id, None)
        if not name:
            return
        bucket = self._name_to_entities.get(name)
        if bucket is None:
            return
        bucket.discard(entity_id)
        if not bucket:
            del self._name_to_entities[name]

    def _add(self, entity_id: str, name: str, qualname: str) -> None:
        if not qualname:
            return
        self.by_qualname[qualname] = entity_id
        self.symbols[qualname] = SymbolInfo(
            entity_id=entity_id, name=name, qualname=qualname
        )
        self._entity_qualname[entity_id] = qualname
        if not name:
            return
        self._entity_name[entity_id] = name
        self._name_to_entities.setdefault(name, set()).add(entity_id)


_IGNORED_TYPE_NAMES = {
    "Any",
    "None",
    "NoneType",
    "Optional",
    "Union",
    "annotations",
    "bool",
    "bytes",
    "dict",
    "float",
    "frozenset",
    "int",
    "list",
    "object",
    "set",
    "str",
    "tuple",
    "typing.Any",
    "typing.Dict",
    "typing.FrozenSet",
    "typing.List",
    "typing.Optional",
    "typing.Sequence",
    "typing.Set",
    "typing.Tuple",
    "typing.Union",
    "Sequence",
}


def build_symbol_maps(conn: sqlite3.Connection, source_base: str) -> SymbolMaps:
    rows = conn.execute(
        """
        SELECT entity_id, name, qualname
        FROM doc_entities
        WHERE source_base = ?
        ORDER BY qualname, entity_id
        """,
        (source_base,),
    ).fetchall()
    by_qualname: dict[str, str] = {}
    symbols: dict[str, SymbolInfo] = {}
    name_counts: dict[str, int] = {}
    name_to_entity_id: dict[str, str] = {}
    for row in rows:
        entity_id = str(row["entity_id"])
        name = str(row["name"] or "")
        qualname = str(row["qualname"] or "")
        if not qualname:
            continue
        by_qualname[qualname] = entity_id
        symbols[qualname] = SymbolInfo(
            entity_id=entity_id,
            name=name,
            qualname=qualname,
        )
        if name:
            name_counts[name] = name_counts.get(name, 0) + 1
            name_to_entity_id[name] = entity_id
    by_unique_name = {
        name: entity_id
        for name, entity_id in name_to_entity_id.items()
        if name_counts.get(name) == 1
    }
    return SymbolMaps(
        by_qualname=by_qualname,
        by_unique_name=by_unique_name,
        symbols=symbols,
    )


def resolve_type_names(type_text: str) -> list[str]:
    seen: set[str] = set()
    names: list[str] = []
    for match in _NAME_RE.finditer(type_text):
        name = match.group(0)
        if _is_ignored_type_name(name):
            continue
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def resolve_symbol_name(
    maps: SymbolMaps, current_qualname: str, name: str
) -> str | None:
    symbol = name.strip()
    if not symbol or _is_ignored_type_name(symbol):
        return None
    entity_id = maps.by_qualname.get(symbol)
    if entity_id is not None:
        return entity_id
    entity_id = maps.by_unique_name.get(symbol)
    if entity_id is not None:
        return entity_id
    for candidate in _relative_qualnames(current_qualname, symbol):
        entity_id = maps.by_qualname.get(candidate)
        if entity_id is not None:
            return entity_id
    return None


def _is_ignored_type_name(name: str) -> bool:
    if name in _IGNORED_TYPE_NAMES:
        return True
    return name.removeprefix("typing.") in _IGNORED_TYPE_NAMES


def _relative_qualnames(current_qualname: str, name: str) -> list[str]:
    if "." in name:
        return []
    parts = [part for part in current_qualname.split(".") if part]
    candidates: list[str] = []
    for i in range(len(parts) - 1, 0, -1):
        candidates.append(".".join([*parts[:i], name]))
    return candidates
