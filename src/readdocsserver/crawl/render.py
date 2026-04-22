"""Parse Sphinx HTML and render main-body text blocks."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, NavigableString, Tag


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


def _is_field_list(tag: Tag) -> bool:
    return tag.name == "dl" and "field-list" in tag.get("class", [])


def _is_py_object(tag: Tag) -> bool:
    return tag.name == "dl" and "py" in tag.get("class", [])


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
