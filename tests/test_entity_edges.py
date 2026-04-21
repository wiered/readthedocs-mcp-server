from __future__ import annotations

import sqlite3
from pathlib import Path

from readdocsserver.store import DocIndex
from readdocsserver.store.docindex.operations import (
    entity_edges,
    entity_edges_between,
    replace_edges,
)
from readdocsserver.store.entity_resolver import (
    build_symbol_maps,
    resolve_symbol_name,
    resolve_type_names,
)
from readdocsserver.store.types import EntityEdge


SOURCE_BASE = "https://docs.example/"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _entity(qualname: str, *, anchor: str | None = None) -> dict:
    name = qualname.rsplit(".", 1)[-1]
    return {
        "local_id": qualname,
        "parent_local_id": None,
        "anchor": anchor or qualname,
        "kind": "class",
        "name": name,
        "qualname": qualname,
        "signature": qualname,
        "summary": f"{qualname}.",
        "body_text": f"{qualname}.",
        "line_start": 1,
        "line_end": 1,
        "params": [],
        "notes": [],
    }


def test_doc_index_initializes_doc_entity_edges_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite"
    DocIndex(db_path)
    DocIndex(db_path)

    with _connect(db_path) as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("doc_entity_edges",),
        ).fetchone()
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }

    assert table is not None
    assert "idx_doc_entity_edges_from" in indexes
    assert "idx_doc_entity_edges_to" in indexes
    assert "idx_doc_entity_edges_source" in indexes
    assert "idx_doc_entity_edges_unique" in indexes


def test_replace_edges_deduplicates_and_reads_edges(tmp_path: Path) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    url = "https://docs.example/api.html"
    idx.upsert_page(
        url,
        "API",
        "View\nLayoutView",
        SOURCE_BASE,
        1,
        [_entity("pkg.View"), _entity("pkg.LayoutView")],
    )
    view_id = f"{url}#pkg.View"
    layout_id = f"{url}#pkg.LayoutView"
    edge = EntityEdge(
        edge_id=None,
        source_base="",
        from_entity_id=layout_id,
        to_entity_id=view_id,
        edge_type="inherits_from",
        source_kind="signature",
    )

    with _connect(idx.db_path) as conn:
        replace_edges(conn, url, SOURCE_BASE, [edge, edge])
        out_edges = entity_edges(conn, layout_id, direction="out")
        in_edges = entity_edges(
            conn, view_id, direction="in", edge_type="inherits_from"
        )
        between = entity_edges_between(conn, layout_id, view_id)

    assert len(out_edges) == 1
    assert len(in_edges) == 1
    assert len(between) == 1
    assert between[0].edge_type == "inherits_from"
    assert between[0].source_base == SOURCE_BASE
    assert between[0].page_url == url


def test_replace_entities_preserves_valid_incoming_and_deletes_stale_edges(
    tmp_path: Path,
) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    target_url = "https://docs.example/target.html"
    ref_url = "https://docs.example/ref.html"
    idx.upsert_page(target_url, "Target", "Target", SOURCE_BASE, 1, [_entity("pkg.Target")])
    idx.upsert_page(ref_url, "Ref", "Ref", SOURCE_BASE, 1, [_entity("pkg.Ref")])
    target_id = f"{target_url}#pkg.Target"
    ref_id = f"{ref_url}#pkg.Ref"
    edge = EntityEdge(
        edge_id=None,
        source_base="",
        from_entity_id=ref_id,
        to_entity_id=target_id,
        edge_type="references",
        source_kind="xref",
        page_url=ref_url,
    )

    with _connect(idx.db_path) as conn:
        replace_edges(conn, ref_url, SOURCE_BASE, [edge])
        conn.commit()

    idx.upsert_page(target_url, "Target", "Target v2", SOURCE_BASE, 2, [_entity("pkg.Target")])
    with _connect(idx.db_path) as conn:
        assert len(entity_edges(conn, target_id, direction="in")) == 1

    idx.upsert_page(target_url, "Target", "Target removed", SOURCE_BASE, 3, [])
    with _connect(idx.db_path) as conn:
        assert entity_edges(conn, target_id, direction="in") == []


def test_clear_source_deletes_entity_edges(tmp_path: Path) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    url = "https://docs.example/api.html"
    idx.upsert_page(
        url,
        "API",
        "A\nB",
        SOURCE_BASE,
        1,
        [_entity("pkg.A"), _entity("pkg.B")],
    )
    edge = EntityEdge(
        edge_id=None,
        source_base="",
        from_entity_id=f"{url}#pkg.A",
        to_entity_id=f"{url}#pkg.B",
        edge_type="references",
        source_kind="xref",
    )
    with _connect(idx.db_path) as conn:
        replace_edges(conn, url, SOURCE_BASE, [edge])
        conn.commit()

    assert idx.clear_source(SOURCE_BASE) == 1
    with _connect(idx.db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM doc_entity_edges").fetchone()[0]

    assert count == 0


def test_resolve_type_names_handles_unions_and_ignores_builtins() -> None:
    assert resolve_type_names("Optional[Union[View, LayoutView]]") == [
        "View",
        "LayoutView",
    ]
    assert resolve_type_names("View | LayoutView | None") == ["View", "LayoutView"]
    assert resolve_type_names("list[str] | typing.Sequence[pkg.Message] | Any") == [
        "pkg.Message"
    ]


def test_resolve_symbol_name_exact_unique_and_relative(tmp_path: Path) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    url = "https://docs.example/api.html"
    idx.upsert_page(
        url,
        "API",
        "Widget\nView\nLayoutView",
        SOURCE_BASE,
        1,
        [
            _entity("pkg.Widget"),
            _entity("pkg.ui.View"),
            _entity("pkg.ui.LayoutView"),
            _entity("alpha.Conflict"),
            _entity("beta.Conflict"),
        ],
    )

    with _connect(idx.db_path) as conn:
        maps = build_symbol_maps(conn, SOURCE_BASE)

    assert resolve_symbol_name(maps, "pkg.ui.LayoutView", "pkg.ui.View") == (
        f"{url}#pkg.ui.View"
    )
    assert resolve_symbol_name(maps, "pkg.ui.LayoutView", "LayoutView") == (
        f"{url}#pkg.ui.LayoutView"
    )
    assert resolve_symbol_name(maps, "pkg.ui.LayoutView.method", "View") == (
        f"{url}#pkg.ui.View"
    )
    assert resolve_symbol_name(maps, "pkg.ui.LayoutView", "Widget") == (
        f"{url}#pkg.Widget"
    )
    assert resolve_symbol_name(maps, "pkg.ui.LayoutView", "Conflict") is None
    assert resolve_symbol_name(maps, "pkg.ui.LayoutView", "str") is None
