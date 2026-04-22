"""Extract nested section TOC from Sphinx HTML pages."""

from __future__ import annotations

from bs4 import Tag

from readdocsserver.crawl.render import _inline_text


def _direct_section_children(root: Tag) -> list[Tag]:
    sections: list[Tag] = []
    for child in root.children:
        if isinstance(child, Tag) and child.name == "section":
            sections.append(child)
    if sections:
        return sections
    for section in root.find_all("section"):
        if not isinstance(section, Tag):
            continue
        parent = section.parent
        has_section_parent = False
        while isinstance(parent, Tag) and parent is not root:
            if parent.name == "section":
                has_section_parent = True
                break
            parent = parent.parent
        if not has_section_parent:
            sections.append(section)
    return sections


def _first_direct_child(tag: Tag, names: set[str]) -> Tag | None:
    for child in tag.children:
        if isinstance(child, Tag) and child.name in names:
            return child
    return None


def _section_to_toc_item(section: Tag, page_url: str, *, level: int) -> dict | None:
    section_id = str(section.get("id") or "").strip()
    if not section_id:
        return None

    heading = _first_direct_child(section, {"h1", "h2", "h3", "h4", "h5", "h6"})
    if heading is None:
        return None

    title = _inline_text(heading)
    if not title:
        return None

    summary_el = _first_direct_child(section, {"p"})
    summary = _inline_text(summary_el) if summary_el is not None else ""
    children: list[dict] = []
    for child_section in _direct_section_children(section):
        child_item = _section_to_toc_item(child_section, page_url, level=level + 1)
        if child_item is not None:
            children.append(child_item)

    return {
        "id": section_id,
        "title": title,
        "summary": summary,
        "level": level,
        "url": f"{page_url}#{section_id}",
        "children": children,
    }


def extract_page_toc(main: Tag, page_url: str) -> list[dict]:
    """Extract a lightweight nested table of contents from Sphinx sections."""
    toc: list[dict] = []
    for section in _direct_section_children(main):
        item = _section_to_toc_item(section, page_url, level=1)
        if item is not None:
            toc.append(item)
    return toc
