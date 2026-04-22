"""Extract structured Python-domain entities from Sphinx HTML."""

from __future__ import annotations

import re
from urllib.parse import urldefrag, urljoin

from bs4 import NavigableString, Tag

from readdocsserver.crawl.render import (
    _inline_text,
    _is_field_list,
    _is_py_object,
    _normalize_inline_text,
    _render_blocks,
    _render_field_list,
    _render_field_list_items,
    _render_text_blocks,
)
from readdocsserver.crawl.urls import canonical_page_url

_PARAM_ITEM_RE = re.compile(
    r"^(?P<name>[A-Za-z_][\w.]*)"
    r"(?:\((?P<type>[^)]*)\))?"
    r"(?:\s*[–-]\s*(?P<description>.*))?$"
)
_VERSION_RE = re.compile(
    r"^(?P<label>Added|Changed|Removed) in version (?P<version>[^:]+):?\s*(?P<text>.*)$"
    r"|^Deprecated since version (?P<deprecated_version>[^:]+):?\s*(?P<deprecated_text>.*)$"
)


def _py_object_kind(dl: Tag) -> str | None:
    classes = set(dl.get("class", []))
    if "class" in classes:
        return "class"
    if classes & {"method", "staticmethod", "classmethod"}:
        return "method"
    return None


def _extract_py_entities(root: Tag, page_url: str) -> list[dict]:
    entities: list[dict] = []

    def walk(node: Tag, parent_local_id: str | None) -> None:
        for child in node.children:
            if not isinstance(child, Tag):
                continue
            if _is_py_object(child):
                entity = _entity_from_py_object(
                    child, parent_local_id, len(entities), page_url
                )
                current_parent = parent_local_id
                if entity is not None:
                    entities.append(entity)
                    current_parent = str(entity["local_id"])
                for dd in child.find_all("dd", recursive=False):
                    walk(dd, current_parent)
                continue
            walk(child, parent_local_id)

    walk(root, None)
    return entities


def _entity_from_py_object(
    dl: Tag, parent_local_id: str | None, ordinal: int, page_url: str
) -> dict | None:
    kind = _py_object_kind(dl)
    if kind is None:
        return None
    sig = dl.find("dt", recursive=False)
    if not isinstance(sig, Tag):
        return None
    signature = _inline_text(sig, compact=True)
    anchor = str(sig.get("id") or "").strip() or None
    name = _signature_name(sig, signature, anchor)
    qualname = anchor or _qualname_from_signature(signature) or name
    local_id = anchor or f"{kind}:{qualname}:{ordinal}"
    dds = [
        child for child in dl.find_all("dd", recursive=False) if isinstance(child, Tag)
    ]
    body_blocks = [_render_entity_body(dd) for dd in dds]
    body_text = "\n\n".join(block for block in body_blocks if block)
    summary = _entity_summary(dds, body_text)
    return {
        "local_id": local_id,
        "parent_local_id": parent_local_id,
        "anchor": anchor,
        "kind": kind,
        "name": name,
        "qualname": qualname,
        "signature": signature,
        "summary": summary,
        "body_text": body_text,
        "line_start": None,
        "line_end": None,
        "params": _extract_entity_params(sig, dds),
        "notes": _extract_entity_notes(dds),
        "xrefs": _extract_entity_xrefs(sig, dds, page_url),
    }


def _signature_name(sig: Tag, signature: str, anchor: str | None) -> str:
    name_el = sig.select_one(".sig-name .pre, .sig-name, .descname .pre, .descname")
    if name_el:
        text = _inline_text(name_el, compact=True)
        if text:
            return text.rsplit(".", 1)[-1]
    qualname = _qualname_from_signature(signature)
    if qualname:
        return qualname.rsplit(".", 1)[-1]
    if anchor:
        return anchor.rsplit(".", 1)[-1]
    return signature.split("(", 1)[0].strip().rsplit(" ", 1)[-1]


def _qualname_from_signature(signature: str) -> str | None:
    head = signature.split("(", 1)[0].strip()
    head = re.sub(
        r"^(async|abstract|abstractmethod|classmethod|staticmethod|class)\s+", "", head
    )
    return head or None


def _extract_entity_params(sig: Tag, dds: list[Tag]) -> list[dict]:
    params = _params_from_signature(sig)
    by_name = {p["name"]: p for p in params}
    for param in _params_from_field_lists(dds):
        existing = by_name.get(param["name"])
        if existing is None:
            by_name[param["name"]] = param
            params.append(param)
            continue
        if param.get("type") and not existing.get("type"):
            existing["type"] = param["type"]
        if param.get("description"):
            existing["description"] = param["description"]
    for i, param in enumerate(params):
        param["ord"] = i
    return params


def _params_from_signature(sig: Tag) -> list[dict]:
    params: list[dict] = []
    for i, param in enumerate(sig.select("em.sig-param")):
        text = _inline_text(param, compact=True)
        if not text:
            continue
        name_part = text.split(":", 1)[0].split("=", 1)[0].strip()
        name = name_part.lstrip("*").strip()
        if not name or name in {"/"}:
            continue
        type_text = ""
        default = ""
        if ":" in text:
            after_colon = text.split(":", 1)[1]
            if "=" in after_colon:
                type_text, default = [
                    part.strip() for part in after_colon.split("=", 1)
                ]
            else:
                type_text = after_colon.strip()
        elif "=" in text:
            default = text.split("=", 1)[1].strip()
        params.append(
            {
                "ord": i,
                "name": name,
                "type": type_text,
                "default": default,
                "description": "",
            }
        )
    return params


def _params_from_field_lists(dds: list[Tag]) -> list[dict]:
    params: list[dict] = []
    for dd in dds:
        for field in dd.find_all("dl", class_="field-list"):
            children = [child for child in field.children if isinstance(child, Tag)]
            i = 0
            while i < len(children):
                dt = children[i]
                value = children[i + 1] if i + 1 < len(children) else None
                i += 2
                if dt.name != "dt" or not isinstance(value, Tag) or value.name != "dd":
                    continue
                label = _inline_text(dt).rstrip(":").lower()
                if label != "parameters":
                    continue
                for item in _render_field_list_items(value):
                    parsed = _parse_parameter_item(item)
                    if parsed:
                        params.append(parsed)
    return params


def _parse_parameter_item(text: str) -> dict | None:
    m = _PARAM_ITEM_RE.match(text.strip())
    if not m:
        return None
    return {
        "ord": 0,
        "name": m.group("name"),
        "type": (m.group("type") or "").strip(),
        "default": "",
        "description": (m.group("description") or "").strip(),
    }


def _extract_entity_notes(dds: list[Tag]) -> list[dict]:
    notes: list[dict] = []
    for dd in dds:
        for node in dd.find_all(["div", "p"], recursive=True):
            if _has_structural_note_ancestor(node, dd):
                continue
            classes = set(node.get("class", []))
            kind = None
            version = ""
            if "warning" in classes:
                kind = "warning"
            elif "note" in classes:
                kind = "note"
            elif "versionadded" in classes:
                kind = "versionadded"
            elif "versionchanged" in classes:
                kind = "versionchanged"
            elif "versionremoved" in classes:
                kind = "versionremoved"
            elif "deprecated" in classes:
                kind = "deprecated"
            elif "admonition" in classes:
                kind = "admonition"
            text = _inline_text(node)
            if not kind:
                m = _VERSION_RE.match(text)
                if m:
                    label = m.group("label")
                    if label == "Added":
                        kind = "versionadded"
                    elif label == "Changed":
                        kind = "versionchanged"
                    elif label == "Removed":
                        kind = "versionremoved"
                    else:
                        kind = "deprecated"
                    version = (
                        m.group("version") or m.group("deprecated_version") or ""
                    ).strip()
                    text = (
                        m.group("text") or m.group("deprecated_text") or ""
                    ).strip() or text
            if kind and text:
                if kind.startswith("version") and not version:
                    m = _VERSION_RE.match(text)
                    if m:
                        version = (
                            m.group("version") or m.group("deprecated_version") or ""
                        ).strip()
                        text = (
                            m.group("text") or m.group("deprecated_text") or ""
                        ).strip() or text
                elif kind == "deprecated" and not version:
                    m = _VERSION_RE.match(text)
                    if m:
                        version = (
                            m.group("version") or m.group("deprecated_version") or ""
                        ).strip()
                        text = (
                            m.group("text") or m.group("deprecated_text") or ""
                        ).strip() or text
                notes.append(
                    {
                        "ord": len(notes),
                        "kind": kind,
                        "version": version,
                        "text": text,
                    }
                )
    return notes


def _extract_entity_xrefs(sig: Tag, dds: list[Tag], page_url: str) -> list[dict]:
    xrefs: list[dict] = []
    for container in [sig, *dds]:
        for link in container.find_all("a", href=True):
            if "headerlink" in link.get("class", []):
                continue
            target_url, target_anchor = _xref_target(str(link["href"]), page_url)
            if not target_anchor:
                continue
            snippet = _xref_snippet(link)
            edge_type = _xref_edge_type(link)
            xrefs.append(
                {
                    "edge_type": edge_type,
                    "source_kind": "xref",
                    "target_url": target_url,
                    "target_anchor": target_anchor,
                    "target_name": _inline_text(link, compact=True),
                    "snippet": snippet,
                    "confidence": 1.0,
                }
            )
    return _dedupe_xrefs(xrefs)


def _xref_target(href: str, page_url: str) -> tuple[str, str]:
    joined = urljoin(page_url, href)
    page_part, fragment = urldefrag(joined)
    return canonical_page_url(page_part), fragment.strip()


def _xref_snippet(link: Tag) -> str:
    parent = link.parent if isinstance(link.parent, Tag) else link
    text = _inline_text(parent)
    if not text:
        text = _inline_text(link, compact=True)
    return text[:300]


def _xref_edge_type(link: Tag) -> str:
    parent = link.parent
    in_note = False
    in_warning = False
    while isinstance(parent, Tag):
        classes = set(parent.get("class", []))
        if "warning" in classes:
            in_warning = True
        if classes & {"note", "admonition", "versionadded", "versionchanged"}:
            in_note = True
        if _is_see_also_context(parent):
            return "see_also"
        parent = parent.parent
    if in_warning:
        return "mentioned_in_warning"
    if in_note:
        return "mentioned_in_note"
    return "references"


def _is_see_also_context(tag: Tag) -> bool:
    text = _inline_text(tag)
    if text.lower().startswith("see also"):
        return True
    previous = tag.find_previous_sibling()
    if isinstance(previous, Tag):
        label = _inline_text(previous).rstrip(":").lower()
        return label == "see also"
    return False


def _dedupe_xrefs(xrefs: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str, str, str]] = set()
    out: list[dict] = []
    for xref in xrefs:
        key = (
            str(xref.get("edge_type") or ""),
            str(xref.get("target_url") or ""),
            str(xref.get("target_anchor") or ""),
            str(xref.get("target_name") or ""),
            str(xref.get("snippet") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(xref)
    return out


def _has_structural_note_ancestor(node: Tag, boundary: Tag) -> bool:
    parent = node.parent
    while isinstance(parent, Tag) and parent is not boundary:
        classes = set(parent.get("class", []))
        if classes & {
            "note",
            "warning",
            "admonition",
            "versionadded",
            "versionchanged",
            "versionremoved",
            "deprecated",
        }:
            return True
        parent = parent.parent
    return False


def _render_entity_body(dd: Tag) -> str:
    blocks: list[str] = []
    for child in dd.children:
        if isinstance(child, NavigableString):
            text = _normalize_inline_text(str(child))
            if text:
                blocks.append(text)
            continue
        if not isinstance(child, Tag) or _is_py_object(child):
            continue
        if _is_field_list(child):
            rendered = _render_field_list(child)
            if rendered:
                blocks.append(rendered)
            continue
        blocks.extend(_render_blocks(child))
    return "\n\n".join(block for block in blocks if block.strip())


def _entity_summary(dds: list[Tag], body_text: str) -> str:
    for dd in dds:
        for child in dd.children:
            if isinstance(child, Tag) and child.name == "p":
                text = _inline_text(child)
                if text:
                    return text
    for line in body_text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _attach_body_lines(entities: list[dict], body: str) -> None:
    lines = body.splitlines()
    used: dict[str, int] = {}
    for entity in entities:
        signature = str(entity.get("signature") or "").strip()
        name = str(entity.get("name") or "").strip()
        needle = signature or name
        if not needle:
            continue
        key = needle.lower()
        start_at = used.get(key, 0)
        for idx in range(start_at, len(lines)):
            line = lines[idx].strip()
            if needle in line or (name and name in line):
                line_no = idx + 1
                entity["line_start"] = line_no
                entity["line_end"] = line_no + max(
                    0, str(entity.get("body_text") or "").count("\n")
                )
                used[key] = idx + 1
                break


def extract_structured_entities(
    main: Tag, page_url: str, body: str | None = None
) -> list[dict]:
    """Extract Sphinx Python-domain classes, methods, parameters, and notes."""
    rendered_body = body if body is not None else _render_text_blocks(main)
    entities = _extract_py_entities(main, page_url)
    _attach_body_lines(entities, rendered_body)
    return entities
