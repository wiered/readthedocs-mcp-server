"""Extract structured Python-domain entities from Sphinx HTML."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import re
from urllib.parse import urldefrag, urljoin

from bs4 import NavigableString, Tag

from readdocsserver.crawl.render import (
    _RenderCache,
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


@dataclass(slots=True)
class _AnalyzedDd:
    body_text: str
    summary: str
    params: list[dict]
    notes: list[dict]
    xrefs: list[dict]


@dataclass(slots=True)
class _EntityContentTag:
    tag: Tag
    xref_edge_type: str


def _py_object_kind(dl: Tag) -> str | None:
    classes = set(dl.get("class", []))
    if "class" in classes:
        return "class"
    if classes & {"method", "staticmethod", "classmethod"}:
        return "method"
    return None


def _extract_py_entities(root: Tag, page_url: str) -> list[dict]:
    entities: list[dict] = []
    dd_cache: dict[int, _AnalyzedDd] = {}
    render_cache: _RenderCache = {}

    def walk(
        node: Tag,
        parent_local_id: str | None,
        use_tqdm: bool = False,
        recurse_level: int = 0,
    ) -> None:
        # Only use tqdm() to wrap node.children if use_tqdm is True
        children_iter = node.children
        if use_tqdm:
            try:
                from tqdm import tqdm

                desk = "Extracting entities: {}".format(node.name)

                children_iter = tqdm(list(node.children), desc=desk)
            except ImportError:
                pass  # tqdm not available; just use default
        for child in children_iter:
            if not isinstance(child, Tag):
                # print("{}not a tag".format("\t" * (recurse_level + 1)))
                continue
            if _is_py_object(child):
                entity = _entity_from_py_object(
                    child,
                    parent_local_id,
                    len(entities),
                    page_url,
                    dd_cache,
                    render_cache,
                )
                current_parent = parent_local_id
                if entity is not None:
                    entities.append(entity)
                    current_parent = str(entity["local_id"])
                for dd in child.find_all("dd", recursive=False):
                    walk(
                        dd,
                        current_parent,
                        use_tqdm=False,
                        recurse_level=recurse_level + 1,
                    )
                continue
            walk(
                child, parent_local_id, use_tqdm=False, recurse_level=recurse_level + 1
            )

    walk(root, None, use_tqdm=True, recurse_level=0)
    return entities


def _entity_from_py_object(
    dl: Tag,
    parent_local_id: str | None,
    ordinal: int,
    page_url: str,
    dd_cache: dict[int, _AnalyzedDd],
    render_cache: _RenderCache,
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
    dd_analyses = [
        _analyze_entity_dd(dd, page_url, dd_cache, render_cache) for dd in dds
    ]
    body_text = "\n\n".join(
        analysis.body_text for analysis in dd_analyses if analysis.body_text
    )
    summary = next(
        (analysis.summary for analysis in dd_analyses if analysis.summary), ""
    )
    field_params = [param for analysis in dd_analyses for param in analysis.params]
    notes = [note.copy() for analysis in dd_analyses for note in analysis.notes]
    for i, note in enumerate(notes):
        note["ord"] = i
    sig_xrefs = _extract_xrefs_from_container(sig, page_url)
    dd_xrefs = [xref for analysis in dd_analyses for xref in analysis.xrefs]
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
        "params": _merge_entity_params(sig, field_params),
        "notes": notes,
        "xrefs": _dedupe_xrefs([*sig_xrefs, *dd_xrefs]),
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


def _merge_entity_params(sig: Tag, field_params: list[dict]) -> list[dict]:
    params = _params_from_signature(sig)
    by_name = {p["name"]: p for p in params}
    for param in field_params:
        existing = by_name.get(param["name"])
        if existing is None:
            new_param = param.copy()
            by_name[new_param["name"]] = new_param
            params.append(new_param)
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


def _params_from_field_list(field: Tag) -> list[dict]:
    params: list[dict] = []
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


def _note_from_node(node: Tag, boundary: Tag, ord_: int) -> dict | None:
    if _has_structural_note_ancestor(node, boundary):
        return None
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
            text = (m.group("text") or m.group("deprecated_text") or "").strip() or text
    if not kind or not text:
        return None
    if (kind.startswith("version") or kind == "deprecated") and not version:
        m = _VERSION_RE.match(text)
        if m:
            version = (
                m.group("version") or m.group("deprecated_version") or ""
            ).strip()
            text = (m.group("text") or m.group("deprecated_text") or "").strip() or text
    return {
        "ord": ord_,
        "kind": kind,
        "version": version,
        "text": text,
    }


def _extract_xrefs_from_container(container: Tag, page_url: str) -> list[dict]:
    xrefs: list[dict] = []
    for link in container.find_all("a", href=True):
        xref = _xref_from_link(link, page_url, edge_type="references")
        if xref is not None:
            xrefs.append(xref)
    return xrefs


def _xref_from_link(
    link: Tag, page_url: str, edge_type: str = "references"
) -> dict | None:
    if "headerlink" in link.get("class", []):
        return None
    target_url, target_anchor = _xref_target(str(link["href"]), page_url)
    if not target_anchor:
        return None
    return {
        "edge_type": edge_type,
        "source_kind": "xref",
        "target_url": target_url,
        "target_anchor": target_anchor,
        "target_name": _inline_text(link, compact=True),
        "snippet": _xref_snippet(link),
        "confidence": 1.0,
    }


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


def _is_see_also_label(tag: Tag) -> bool:
    return _inline_text(tag, compact=True).rstrip(":").lower() == "see also"


def _starts_with_see_also_label(tag: Tag) -> bool:
    text = ""
    limit = len("see also:") + 1
    for value in tag.strings:
        text = _normalize_inline_text(f"{text} {value}" if text else str(value))
        if len(text) >= limit:
            break
    label = text[:limit].lower().lstrip()
    return label.startswith("see also")


def _has_see_also_title(tag: Tag) -> bool:
    for child in tag.children:
        if not isinstance(child, Tag):
            continue
        if "admonition-title" in child.get("class", []):
            return _is_see_also_label(child)
    return False


def _is_see_also_node(tag: Tag, follows_see_also_dt: bool) -> bool:
    classes = set(tag.get("class", []))
    return (
        follows_see_also_dt
        or "seealso" in classes
        or (tag.name == "p" and _starts_with_see_also_label(tag))
        or ("admonition" in classes and _has_see_also_title(tag))
    )


def _xref_edge_type_for_tag(
    tag: Tag, inherited_edge_type: str, follows_see_also_dt: bool
) -> str:
    if inherited_edge_type == "see_also" or _is_see_also_node(tag, follows_see_also_dt):
        return "see_also"
    classes = set(tag.get("class", []))
    if "warning" in classes:
        return "mentioned_in_warning"
    if classes & {"note", "admonition", "versionadded", "versionchanged"}:
        return "mentioned_in_note"
    return inherited_edge_type


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


def _render_entity_body(dd: Tag, render_cache: _RenderCache) -> str:
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
            rendered = _render_field_list(
                child, cache=render_cache, include_py_objects=False
            )
            if rendered:
                blocks.append(rendered)
            continue
        blocks.extend(
            _render_blocks(child, cache=render_cache, include_py_objects=False)
        )
    return "\n\n".join(block for block in blocks if block.strip())


def _analyze_entity_dd(
    dd: Tag,
    page_url: str,
    cache: dict[int, _AnalyzedDd],
    render_cache: _RenderCache,
) -> _AnalyzedDd:
    cache_key = id(dd)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    body_text = _render_entity_body(dd, render_cache)
    summary = _summary_from_dd(dd) or _summary_from_body(body_text)
    params: list[dict] = []
    notes: list[dict] = []
    xrefs: list[dict] = []

    for item in _iter_entity_content_tags(dd):
        node = item.tag
        if _is_field_list(node):
            params.extend(_params_from_field_list(node))
        if node.name in {"div", "p"}:
            note = _note_from_node(node, dd, len(notes))
            if note is not None:
                notes.append(note)
        if node.name == "a" and node.has_attr("href"):
            xref = _xref_from_link(node, page_url, edge_type=item.xref_edge_type)
            if xref is not None:
                xrefs.append(xref)

    analyzed = _AnalyzedDd(
        body_text=body_text,
        summary=summary,
        params=params,
        notes=notes,
        xrefs=_dedupe_xrefs(xrefs),
    )
    cache[cache_key] = analyzed
    return analyzed


def _iter_entity_content_tags(dd: Tag) -> Iterator[_EntityContentTag]:
    stack = list(reversed(_child_content_tags(dd, "references")))
    while stack:
        item = stack.pop()
        node = item.tag
        if _is_py_object(node):
            continue
        yield item
        stack.extend(reversed(_child_content_tags(node, item.xref_edge_type)))


def _child_content_tags(parent: Tag, edge_type: str) -> list[_EntityContentTag]:
    items: list[_EntityContentTag] = []
    follows_see_also_dt = False
    for child in parent.children:
        if not isinstance(child, Tag):
            continue
        child_edge_type = _xref_edge_type_for_tag(
            child, edge_type, follows_see_also_dt and child.name == "dd"
        )
        items.append(_EntityContentTag(tag=child, xref_edge_type=child_edge_type))
        follows_see_also_dt = child.name == "dt" and _is_see_also_label(child)
    return items


def _summary_from_dd(dd: Tag) -> str:
    for child in dd.children:
        if isinstance(child, Tag) and child.name == "p":
            text = _inline_text(child)
            if text:
                return text
    return ""


def _summary_from_body(body_text: str) -> str:
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
