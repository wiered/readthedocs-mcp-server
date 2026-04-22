"""Index a Read the Docs / Sphinx site into the local DocIndex."""

from __future__ import annotations

from readdocsserver.crawl import crawl_readthedocs, normalize_doc_root
from readdocsserver.schemas.mcp import IndexStats
from readdocsserver.services.deps import get_doc_index
from readdocsserver.utils.validators import (
    validate_delay,
    validate_max_pages,
    validate_seed_url,
)


async def run_index_readthedocs(
    seed_url: str,
    max_pages: int = 200,
    request_delay_sec: float = 0.15,
    replace_source: bool = True,
) -> IndexStats:
    validated_seed_url = validate_seed_url(seed_url)
    validated_max_pages = validate_max_pages(max_pages)
    validated_delay = validate_delay(request_delay_sec)

    idx = get_doc_index()
    root = normalize_doc_root(validated_seed_url)
    if replace_source:
        idx.clear_source(root)

    async def on_page(
        url: str,
        title: str,
        body: str,
        source_base: str,
        fetched_at: int,
        entities: list[dict],
        toc: list[dict],
    ) -> None:
        idx.upsert_page(url, title, body, source_base, fetched_at, entities, toc)

    stats = await crawl_readthedocs(
        validated_seed_url,
        max_pages=validated_max_pages,
        request_delay_sec=validated_delay,
        on_page=on_page,
    )
    idx.rebuild_graph_for_source(root)
    return IndexStats.model_validate({**stats, "db_path": str(idx.db_path)})
