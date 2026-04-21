"""Crawl Read the Docs / Sphinx HTML under a documentation root URL."""

from __future__ import annotations

import asyncio
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, NavigableString, Tag
from curl_cffi.requests import AsyncSession

SKIP_EXTENSIONS = (
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp4",
    ".webm",
    ".js",
    ".css",
)


def normalize_doc_root(url: str) -> str:
    """Turn a doc entry URL into a directory prefix (trailing slash)."""
    raw = url.strip()
    p = urlparse(raw)
    path = p.path or "/"
    if not path.endswith("/"):
        last = path.rsplit("/", 1)[-1]
        if "." in last:
            path = path.rsplit("/", 1)[0] + "/"
        else:
            path = path + "/"
    return urlunparse((p.scheme, p.netloc, path, "", "", ""))


def normalize_page_url(url: str) -> str:
    p = urlparse(urldefrag(url)[0])
    return urlunparse((p.scheme, p.netloc, p.path or "/", p.params, p.query, ""))


def canonical_page_url(url: str) -> str:
    """
    Normalize URLs for dedup and queueing so the same doc is not fetched twice.

    Strips Sphinx/RTD ``highlight=`` query noise and collapses ``.../index.html``
    to the directory form used on many Read the Docs builds.
    """
    u = normalize_page_url(url)
    p = urlparse(u)
    path = p.path or "/"
    low = path.lower()
    if low.endswith("/index.html"):
        # ``/dir/index.html`` → ``/dir/``
        path = path[: -len("index.html")]
    elif low == "/index.html":
        path = "/"
    pairs = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() != "highlight"
    ]
    query = urlencode(pairs)
    return urlunparse((p.scheme, p.netloc, path, p.params, query, ""))


_SITEMAP_NS_RE = re.compile(r"^\{([^}]+)\}")


def _sitemap_tag_local_name(tag: str) -> str:
    m = _SITEMAP_NS_RE.match(tag)
    return tag[m.end() :] if m else tag


def _parse_sitemap_urls(xml_bytes: bytes) -> tuple[list[str], list[str]]:
    """
    Return (page_locs, nested_sitemap_locs) from a sitemap or sitemap index document.
    """
    page_locs: list[str] = []
    nested: list[str] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return page_locs, nested
    root_name = _sitemap_tag_local_name(root.tag).lower()
    for el in root.iter():
        name = _sitemap_tag_local_name(el.tag).lower()
        if name != "loc":
            continue
        text = (el.text or "").strip()
        if not text:
            continue
        if root_name == "sitemapindex":
            nested.append(text)
        else:
            page_locs.append(text)
    return page_locs, nested


async def discover_sitemap_seed_urls(
    client: AsyncSession,
    doc_root: str,
    origin: str,
    path_prefix: str,
    *,
    max_urls: int,
) -> list[str]:
    """
    Pull URLs from ``sitemap.xml`` (and one level of sitemap index) under ``doc_root``.

    Sphinx often emits a complete sitemap; seeding the crawl avoids missing deep pages
    when ``max_pages`` is modest and the sidebar lists hundreds of siblings before
    links to deeper sections appear in the HTML parse order.
    """
    out: list[str] = []
    seen: set[str] = set()
    to_fetch = [urljoin(doc_root, "sitemap.xml")]
    index_fetches = 0
    while to_fetch and len(out) < max_urls and index_fetches < 8:
        sm_url = to_fetch.pop(0)
        c_sm = canonical_page_url(sm_url)
        if c_sm in seen:
            continue
        seen.add(c_sm)
        index_fetches += 1
        try:
            resp = await client.get(sm_url, timeout=30, allow_redirects=True)
        except Exception:
            continue
        if resp.status_code >= 400:
            continue
        ctype = resp.headers.get("content-type", "").lower()
        raw = resp.content.lstrip()
        looks_like_sitemap = (
            raw.startswith(b"<?xml")
            or raw.startswith(b"<urlset")
            or raw.startswith(b"<sitemapindex")
        )
        if (
            "xml" not in ctype
            and not sm_url.lower().endswith(".xml")
            and not looks_like_sitemap
        ):
            continue
        pages, nested = _parse_sitemap_urls(resp.content)
        for n in nested:
            cn = canonical_page_url(n)
            if cn not in seen and len(to_fetch) < 32:
                to_fetch.append(n)
        for loc in pages:
            joined = normalize_page_url(loc)
            if not under_prefix(joined, origin, path_prefix):
                continue
            cj = canonical_page_url(joined)
            if cj in seen:
                continue
            seen.add(cj)
            out.append(joined)
            if len(out) >= max_urls:
                break
    return out


def under_prefix(page_url: str, origin: str, path_prefix: str) -> bool:
    p = urlparse(page_url)
    if f"{p.scheme}://{p.netloc}" != origin:
        return False
    base = path_prefix.rstrip("/")
    path = (p.path or "/").rstrip("/") or "/"
    if path == base:
        return True
    return path.startswith(base + "/")


def extract_text_and_title(html: bytes, page_url: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for tag in soup.select("a.headerlink"):
        tag.decompose()
    title_el = soup.find("title")
    title = title_el.get_text(strip=True) if title_el else ""
    main = _find_main_content(soup)
    text = _render_text_blocks(main)
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else urlparse(page_url).path
    return title, text


def extract_structured_entities(
    html: bytes, page_url: str, body: str | None = None
) -> list[dict]:
    """Extract Sphinx Python-domain classes, methods, parameters, and notes."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for tag in soup.select("a.headerlink"):
        tag.decompose()
    _ = page_url
    main = _find_main_content(soup)
    rendered_body = body if body is not None else _render_text_blocks(main)
    entities = _extract_py_entities(main)
    _attach_body_lines(entities, rendered_body)
    return entities


def _find_main_content(soup: BeautifulSoup) -> Tag:
    for sel in (
        "div.rst-content",
        "div[itemprop='articleBody']",
        "div.document",
        "article",
        "main",
        '[role="main"]',
    ):
        main = soup.select_one(sel)
        if main:
            return main
    return soup.body or soup


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize_inline_text(text: str) -> str:
    text = _collapse_ws(text.replace("¶", " "))
    if not text:
        return ""
    text = re.sub(r"\s+([,.;!?%)\]\}])", r"\1", text)
    text = re.sub(r"([(\[\{])\s+", r"\1", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"\s*:\s*", ": ", text)
    text = re.sub(r"\s*([=|→])\s*", r" \1 ", text)
    text = re.sub(r"\)\s*[–-]\s*", ") – ", text)
    text = re.sub(r"\s+[–-]\s+", " – ", text)
    text = re.sub(r":\s+//", "://", text)
    text = re.sub(r"(?<=[A-Za-z0-9_])\.\s+(?=[A-Za-z0-9_])", ".", text)
    return _collapse_ws(text)


def _inline_text(tag: Tag, *, compact: bool = False) -> str:
    text = _normalize_inline_text(tag.get_text(" ", strip=True))
    if compact:
        text = re.sub(r"\s+([(\[\{])", r"\1", text)
        text = _collapse_ws(text)
    return text


def _is_sig_object(tag: Tag) -> bool:
    classes = set(tag.get("class", []))
    return tag.name == "dt" and {"sig", "py"}.issubset(classes)


def _is_field_list(tag: Tag) -> bool:
    return tag.name == "dl" and "field-list" in tag.get("class", [])


def _is_py_object(tag: Tag) -> bool:
    return tag.name == "dl" and "py" in tag.get("class", [])


def _py_object_kind(dl: Tag) -> str | None:
    classes = set(dl.get("class", []))
    if "class" in classes:
        return "class"
    if classes & {"method", "staticmethod", "classmethod"}:
        return "method"
    return None


def _render_text_blocks(root: Tag) -> str:
    blocks = _render_blocks(root)
    return "\n\n".join(block for block in blocks if block.strip())


def _render_blocks(node: Tag) -> list[str]:
    blocks: list[str] = []
    for child in node.children:
        if isinstance(child, NavigableString):
            text = _normalize_inline_text(str(child))
            if text:
                blocks.append(text)
            continue
        if not isinstance(child, Tag):
            continue
        if _is_py_object(child):
            blocks.extend(_render_py_object(child))
            continue
        if _is_field_list(child):
            rendered = _render_field_list(child)
            if rendered:
                blocks.append(rendered)
            continue
        if child.name in {"section", "article", "main", "div", "dd", "body"}:
            blocks.extend(_render_blocks(child))
            continue
        if child.name in {"h1", "h2", "h3", "h4", "h5", "h6", "p"}:
            text = _inline_text(child)
            if text:
                blocks.append(text)
            continue
        if child.name in {"ul", "ol"}:
            blocks.extend(_render_list(child))
            continue
        if child.name == "pre":
            text = child.get_text("\n", strip=False).strip()
            if text:
                blocks.append(text)
            continue
        text = _inline_text(child)
        if text:
            blocks.append(text)
    return blocks


def _render_py_object(dl: Tag) -> list[str]:
    blocks: list[str] = []
    sig = dl.find("dt", recursive=False)
    if sig and isinstance(sig, Tag):
        sig_text = _inline_text(sig, compact=True)
        if sig_text:
            blocks.append(sig_text)
    for child in dl.find_all(recursive=False):
        if child.name != "dd":
            continue
        blocks.extend(_render_blocks(child))
    return blocks


def _render_list(list_tag: Tag) -> list[str]:
    blocks: list[str] = []
    for li in list_tag.find_all("li", recursive=False):
        nested = [child for child in li.children if isinstance(child, Tag)]
        paras = [child for child in nested if child.name == "p"]
        if paras:
            for p in paras:
                text = _inline_text(p)
                if text:
                    blocks.append(text)
            for child in nested:
                if child.name == "p":
                    continue
                if child.name in {"ul", "ol"}:
                    blocks.extend(_render_list(child))
        else:
            text = _inline_text(li)
            if text:
                blocks.append(text)
    return blocks


def _render_field_list(dl: Tag) -> str:
    lines: list[str] = []
    children = [child for child in dl.children if isinstance(child, Tag)]
    i = 0
    while i < len(children):
        dt = children[i]
        dd = children[i + 1] if i + 1 < len(children) else None
        i += 2
        if dt.name != "dt" or dd is None or dd.name != "dd":
            continue
        label = _inline_text(dt).rstrip(":")
        if not label:
            continue
        if label.lower() == "parameters":
            items = _render_field_list_items(dd)
            if items:
                lines.append(f"{label}:")
                lines.extend(items)
            continue
        value_blocks = _render_blocks(dd)
        if not value_blocks:
            value = _inline_text(dd)
            if value:
                value_blocks = [value]
        if value_blocks:
            lines.append(f"{label}:")
            lines.extend(value_blocks)
    return "\n".join(lines).strip()


def _render_field_list_items(dd: Tag) -> list[str]:
    items: list[str] = []
    for ul in dd.find_all(["ul", "ol"], recursive=False):
        for item in _render_list(ul):
            items.append(re.sub(r"^([A-Za-z_][\w.]*)\s+\(", r"\1(", item))
    if items:
        return items
    text = _inline_text(dd)
    return [text] if text else []


_PARAM_ITEM_RE = re.compile(
    r"^(?P<name>[A-Za-z_][\w.]*)"
    r"(?:\((?P<type>[^)]*)\))?"
    r"(?:\s*[–-]\s*(?P<description>.*))?$"
)
_VERSION_RE = re.compile(
    r"^(?P<label>Added|Changed|Removed) in version (?P<version>[^:]+):?\s*(?P<text>.*)$"
    r"|^Deprecated since version (?P<deprecated_version>[^:]+):?\s*(?P<deprecated_text>.*)$"
)


def _extract_py_entities(root: Tag) -> list[dict]:
    entities: list[dict] = []

    def walk(node: Tag, parent_local_id: str | None) -> None:
        for child in node.children:
            if not isinstance(child, Tag):
                continue
            if _is_py_object(child):
                entity = _entity_from_py_object(child, parent_local_id, len(entities))
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
    dl: Tag, parent_local_id: str | None, ordinal: int
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
    dds = [child for child in dl.find_all("dd", recursive=False) if isinstance(child, Tag)]
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
    head = re.sub(r"^(async|abstract|abstractmethod|classmethod|staticmethod|class)\s+", "", head)
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
                type_text, default = [part.strip() for part in after_colon.split("=", 1)]
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
                entity["line_end"] = line_no + max(0, str(entity.get("body_text") or "").count("\n"))
                used[key] = idx + 1
                break


_HEAD_RELS = frozenset(
    {
        "next",
        "prev",
        "up",
        "chapter",
        "first",
        "last",
        "index",
        "appendix",
        "help",
        "contents",
        "toc",
        "start",
        "top",
        "subsection",
        "section",
        "bookmark",
    }
)


def same_site_links(
    html: bytes, base_url: str, origin: str, path_prefix: str
) -> list[str]:
    """Collect same-site doc URLs from anchors and Sphinx/RTD ``<link rel="...">`` navigation."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []

    def consider_href(href: str) -> None:
        joined = normalize_page_url(urljoin(base_url, href))
        p = urlparse(joined)
        if p.scheme not in ("http", "https"):
            return
        low = joined.lower()
        if any(low.endswith(ext) for ext in SKIP_EXTENSIONS):
            return
        if under_prefix(joined, origin, path_prefix):
            out.append(joined)

    for a in soup.find_all("a", href=True):
        consider_href(a["href"])
    for link in soup.find_all("link", href=True):
        rel = link.get("rel")
        if not rel:
            continue
        rel_parts = {str(r).lower() for r in (rel if isinstance(rel, list) else [rel])}
        if rel_parts & _HEAD_RELS:
            consider_href(link["href"])
    return out


async def crawl_readthedocs(
    seed_url: str,
    *,
    max_pages: int = 200,
    request_delay_sec: float = 0.15,
    impersonate: str | None = None,
    on_page=None,
) -> dict:
    """
    BFS crawl starting from seed_url, staying under the documentation root directory.

    on_page: optional async callback(url, title, body, source_base, fetched_at, entities)
    for each stored page.
    """
    root = normalize_doc_root(seed_url)
    p = urlparse(root)
    origin = f"{p.scheme}://{p.netloc}"
    path_prefix = p.path if p.path.endswith("/") else p.path + "/"

    seed = normalize_page_url(seed_url.strip())
    if under_prefix(seed, origin, path_prefix):
        first = seed
    else:
        first = normalize_page_url(urljoin(root, "index.html"))

    visited: set[str] = set()
    queued: set[str] = set()
    queue: deque[str] = deque()

    def try_enqueue(raw_url: str) -> None:
        ju = normalize_page_url(raw_url.strip())
        if not under_prefix(ju, origin, path_prefix):
            return
        cc = canonical_page_url(ju)
        if cc in visited or cc in queued:
            return
        queued.add(cc)
        queue.append(ju)

    stats: dict = {
        "source_base": root,
        "fetched": 0,
        "skipped": 0,
        "errors": [],
        "sitemap_seeds": 0,
    }

    tls_profile = impersonate or os.environ.get("READTHEDOCS_MCP_IMPERSONATE", "chrome")
    # curl_cffi impersonates a real browser TLS stack; many RTD sites sit behind
    # Cloudflare and return 403 to generic Python HTTP clients (e.g. httpx/requests).
    async with AsyncSession(impersonate=tls_profile) as client:
        sitemap_cap = min(max(max_pages * 8, 500), 20000)
        sitemap_urls = await discover_sitemap_seed_urls(
            client,
            root,
            origin,
            path_prefix,
            max_urls=sitemap_cap,
        )
        stats["sitemap_seeds"] = len(sitemap_urls)

        try_enqueue(first)
        for u in sitemap_urls:
            try_enqueue(u)

        while queue and stats["fetched"] < max_pages:
            current = queue.popleft()
            cc = canonical_page_url(current)
            queued.discard(cc)
            if cc in visited:
                continue
            visited.add(cc)
            fetch_url = cc

            await asyncio.sleep(request_delay_sec)

            try:
                resp = await client.get(fetch_url, timeout=30, allow_redirects=True)
            except Exception as e:
                stats["errors"].append({"url": fetch_url, "error": str(e)})
                continue

            ctype = resp.headers.get("content-type", "").lower()
            if resp.status_code >= 400:
                stats["errors"].append(
                    {"url": fetch_url, "error": f"HTTP {resp.status_code}"}
                )
                continue
            if "text/html" not in ctype and not fetch_url.endswith((".html", "/")):
                stats["skipped"] += 1
                continue

            title, body = extract_text_and_title(resp.content, str(resp.url))
            entities = extract_structured_entities(resp.content, str(resp.url), body)
            final_url = canonical_page_url(normalize_page_url(str(resp.url)))
            if not under_prefix(final_url, origin, path_prefix):
                stats["skipped"] += 1
                continue

            stats["fetched"] += 1
            fetched_at = int(time.time())
            if on_page:
                await on_page(final_url, title, body, root, fetched_at, entities)

            for link in same_site_links(
                resp.content, str(resp.url), origin, path_prefix
            ):
                try_enqueue(link)

    return stats
