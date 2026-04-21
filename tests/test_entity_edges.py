from __future__ import annotations

import sqlite3
from pathlib import Path

from readdocsserver.store import DocIndex
from readdocsserver.store.docindex.operations import (
    entity_edges,
    entity_edges_between,
    replace_edges,
)
from readdocsserver.store.entity_edges import build_edges_for_page
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
        "xrefs": [],
    }


def _method(
    qualname: str,
    parent_local_id: str,
    *,
    signature: str | None = None,
    params: list[dict] | None = None,
) -> dict:
    name = qualname.rsplit(".", 1)[-1]
    return {
        "local_id": qualname,
        "parent_local_id": parent_local_id,
        "anchor": qualname,
        "kind": "method",
        "name": name,
        "qualname": qualname,
        "signature": signature or f"{name}()",
        "summary": f"{qualname}.",
        "body_text": f"{qualname}.",
        "line_start": 2,
        "line_end": 3,
        "params": params or [],
        "notes": [],
        "xrefs": [],
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
    idx.upsert_page(
        target_url, "Target", "Target", SOURCE_BASE, 1, [_entity("pkg.Target")]
    )
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

    idx.upsert_page(
        target_url, "Target", "Target v2", SOURCE_BASE, 2, [_entity("pkg.Target")]
    )
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


def test_build_edges_for_page_creates_has_method(tmp_path: Path) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    url = "https://docs.example/api.html"
    idx.upsert_page(
        url,
        "API",
        "LayoutView\nedit_message",
        SOURCE_BASE,
        1,
        [
            _entity("pkg.LayoutView"),
            _method("pkg.LayoutView.edit_message", "pkg.LayoutView"),
        ],
    )

    with _connect(idx.db_path) as conn:
        edges = entity_edges(conn, f"{url}#pkg.LayoutView", edge_type="has_method")

    assert len(edges) == 1
    assert edges[0].to_entity_id == f"{url}#pkg.LayoutView.edit_message"
    assert edges[0].source_kind == "structure"


def test_build_edges_for_page_creates_returns_and_parameter_types(
    tmp_path: Path,
) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    url = "https://docs.example/api.html"
    idx.upsert_page(
        url,
        "API",
        "Message\nView\nLayoutView\nedit",
        SOURCE_BASE,
        1,
        [
            _entity("pkg.Message"),
            _entity("pkg.View"),
            _entity("pkg.LayoutView"),
            _method(
                "pkg.Message.edit",
                "pkg.Message",
                signature="edit(view: View | LayoutView | None) -> Message",
                params=[
                    {
                        "ord": 0,
                        "name": "view",
                        "type": "View | LayoutView | None",
                        "default": "",
                        "description": "",
                    }
                ],
            ),
        ],
    )

    method_id = f"{url}#pkg.Message.edit"
    with _connect(idx.db_path) as conn:
        return_edges = entity_edges(conn, method_id, edge_type="returns")
        param_edges = entity_edges(conn, method_id, edge_type="accepts_parameter_type")

    assert [edge.to_entity_id for edge in return_edges] == [f"{url}#pkg.Message"]
    assert {edge.to_entity_id for edge in param_edges} == {
        f"{url}#pkg.View",
        f"{url}#pkg.LayoutView",
    }
    assert {edge.param_name for edge in param_edges} == {"view"}


def test_build_edges_for_page_creates_inherits_from(tmp_path: Path) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    url = "https://docs.example/api.html"
    child = _entity("pkg.LayoutView")
    child["signature"] = "class pkg.LayoutView(View)"
    idx.upsert_page(
        url,
        "API",
        "View\nLayoutView",
        SOURCE_BASE,
        1,
        [_entity("pkg.View"), child],
    )

    with _connect(idx.db_path) as conn:
        edges = entity_edges(conn, f"{url}#pkg.LayoutView", edge_type="inherits_from")

    assert len(edges) == 1
    assert edges[0].to_entity_id == f"{url}#pkg.View"
    assert edges[0].snippet == "View"


def test_build_edges_for_page_skips_unresolved_and_other_sources(
    tmp_path: Path,
) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    other_url = "https://other.example/api.html"
    url = "https://docs.example/api.html"
    idx.upsert_page(
        other_url,
        "Other",
        "External",
        "https://other.example/",
        1,
        [_entity("pkg.External")],
    )
    idx.upsert_page(
        url,
        "API",
        "send",
        SOURCE_BASE,
        1,
        [
            {
                **_entity("pkg.Service"),
                "signature": "class pkg.Service(MissingBase)",
            },
            _method(
                "pkg.Service.send",
                "pkg.Service",
                signature="send(value: External) -> MissingReturn",
                params=[
                    {
                        "ord": 0,
                        "name": "value",
                        "type": "External",
                        "default": "",
                        "description": "",
                    }
                ],
            ),
        ],
    )

    with _connect(idx.db_path) as conn:
        edges = build_edges_for_page(conn, SOURCE_BASE, url)

    assert [edge.edge_type for edge in edges] == ["has_method"]


def test_upsert_page_replaces_edges_without_duplicates(tmp_path: Path) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    url = "https://docs.example/api.html"
    entities = [
        _entity("pkg.Message"),
        _method("pkg.Message.copy", "pkg.Message", signature="copy() -> Message"),
    ]

    idx.upsert_page(url, "API", "Message\ncopy", SOURCE_BASE, 1, entities)
    idx.upsert_page(url, "API", "Message\ncopy v2", SOURCE_BASE, 2, entities)

    with _connect(idx.db_path) as conn:
        edges = entity_edges(conn, f"{url}#pkg.Message.copy", edge_type="returns")

    assert len(edges) == 1


def test_upsert_page_links_to_entity_from_other_page_same_source(
    tmp_path: Path,
) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    msg_url = "https://docs.example/message.html"
    api_url = "https://docs.example/api.html"
    idx.upsert_page(
        msg_url, "Message", "Message", SOURCE_BASE, 1, [_entity("pkg.Message")]
    )
    idx.upsert_page(
        api_url,
        "API",
        "send",
        SOURCE_BASE,
        1,
        [
            _entity("pkg.Service"),
            _method("pkg.Service.send", "pkg.Service", signature="send() -> Message"),
        ],
    )

    with _connect(idx.db_path) as conn:
        edges = entity_edges(conn, f"{api_url}#pkg.Service.send", edge_type="returns")

    assert len(edges) == 1
    assert edges[0].to_entity_id == f"{msg_url}#pkg.Message"


def test_upsert_page_creates_xref_edges_and_ignores_page_only_links(
    tmp_path: Path,
) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    url = "https://docs.example/api.html"
    source = _entity("pkg.Source")
    source["xrefs"] = [
        {
            "edge_type": "see_also",
            "source_kind": "xref",
            "target_url": url,
            "target_anchor": "pkg.Target",
            "target_name": "Target",
            "snippet": "See also Target.",
        },
        {
            "edge_type": "references",
            "source_kind": "xref",
            "target_url": "https://docs.example/guide.html",
            "target_anchor": "",
            "target_name": "",
            "snippet": "Guide page.",
        },
    ]
    idx.upsert_page(
        url,
        "API",
        "Source\nTarget",
        SOURCE_BASE,
        1,
        [source, _entity("pkg.Target")],
    )

    with _connect(idx.db_path) as conn:
        edges = entity_edges(conn, f"{url}#pkg.Source", direction="out")

    assert [(edge.edge_type, edge.to_entity_id) for edge in edges] == [
        ("see_also", f"{url}#pkg.Target")
    ]
    assert edges[0].source_kind == "xref"
    assert edges[0].snippet == "See also Target."


def test_rebuild_graph_for_source_resolves_late_xref_target(tmp_path: Path) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    ref_url = "https://docs.example/ref.html"
    target_url = "https://docs.example/target.html"
    source = _entity("pkg.Source")
    source["xrefs"] = [
        {
            "edge_type": "references",
            "source_kind": "xref",
            "target_url": target_url,
            "target_anchor": "pkg.Target",
            "target_name": "Target",
            "snippet": "Later target.",
        }
    ]
    idx.upsert_page(ref_url, "Ref", "Source", SOURCE_BASE, 1, [source])
    idx.upsert_page(
        target_url, "Target", "Target", SOURCE_BASE, 1, [_entity("pkg.Target")]
    )

    with _connect(idx.db_path) as conn:
        before = entity_edges(conn, f"{ref_url}#pkg.Source", direction="out")

    idx.rebuild_graph_for_source(SOURCE_BASE)

    with _connect(idx.db_path) as conn:
        after = entity_edges(conn, f"{ref_url}#pkg.Source", direction="out")

    assert before == []
    assert len(after) == 1
    assert after[0].to_entity_id == f"{target_url}#pkg.Target"


def test_text_inference_edges_resolve_known_targets(tmp_path: Path) -> None:
    idx = DocIndex(tmp_path / "db.sqlite")
    url = "https://docs.example/api.html"
    source = _entity("pkg.Action")
    source["body_text"] = (
        "This can only be used in Guild. You must be used with Client. "
        "Use Replacement instead. Similar to Sibling. It is converted to Message."
    )
    idx.upsert_page(
        url,
        "API",
        "Action\nGuild\nClient\nReplacement\nSibling\nMessage",
        SOURCE_BASE,
        1,
        [
            source,
            _entity("pkg.Guild"),
            _entity("pkg.Client"),
            _entity("pkg.Replacement"),
            _entity("pkg.Sibling"),
            _entity("pkg.Message"),
        ],
    )

    with _connect(idx.db_path) as conn:
        edges = entity_edges(conn, f"{url}#pkg.Action", direction="out")

    assert {edge.edge_type for edge in edges} >= {
        "only_valid_in",
        "requires",
        "use_instead",
        "similar_to",
        "converts_to",
    }
    assert all(edge.source_kind == "text_inference" for edge in edges)
    assert all(edge.confidence < 1.0 for edge in edges)
