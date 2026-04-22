"""Живые pytest-тесты: индексация реального RTD и проверка MCP-инструментов.

Требуют сеть. Включение::

    set READTHEDOCS_MCP_RUN_LIVE=1
    pytest tests/live_test.py -v

Параметры окружения (необязательно)::

    READTHEDOCS_LIVE_MAX_PAGES   — верхняя граница страниц при обходе (по умолчанию 50)
    READTHEDOCS_LIVE_DELAY_SEC   — пауза между HTTP-запросами (по умолчанию 0.05)

Используется отдельный SQLite во временной директории pytest (`tmp_path`), не трогая
кэш пользователя по умолчанию.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import pytest

from readdocsserver.crawl import normalize_doc_root
from readdocsserver.main import create_server
from readdocsserver.schemas.mcp import (
    EntityDetailResponse,
    EntityMethodListResponse,
    EntitySearchResponse,
    FetchResponse,
    IndexStats,
    ListPagesResponse,
    LookupSymbolResponse,
    RelatedSymbolsResponse,
    SearchResponse,
    SourceListResponse,
    SymbolGraphStatsResponse,
)


SEED_URL = "https://discordpy.readthedocs.io/en/stable/index.html"


def _tool_structured(result: Any) -> dict[str, Any]:
    assert isinstance(result, tuple) and len(result) == 2, result
    _, data = result
    assert isinstance(data, dict), type(data)
    return data


def _live_enabled() -> bool:
    return True
    return os.environ.get("READTHEDOCS_MCP_RUN_LIVE", "").strip() == "1"


requires_live = pytest.mark.skipif(
    not _live_enabled(),
    reason="Set READTHEDOCS_MCP_RUN_LIVE=1 to run live discord.py crawl tests (network).",
)


@pytest.mark.live
@requires_live
def test_discordpy_docs_index_and_mcp_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    db_path = tmp_path / "live_readdocs.sqlite"
    monkeypatch.setenv("READTHEDOCS_MCP_DB", str(db_path))

    max_pages = int(os.environ.get("READTHEDOCS_LIVE_MAX_PAGES", "50"))
    delay = float(os.environ.get("READTHEDOCS_LIVE_DELAY_SEC", "0.05"))

    async def _run() -> None:
        m = create_server()
        expected_root = normalize_doc_root(SEED_URL)

        index_raw = _tool_structured(
            await m.call_tool(
                "index_readthedocs",
                {
                    "seed_url": SEED_URL,
                    "max_pages": max_pages,
                    "request_delay_sec": delay,
                    "replace_source": True,
                },
            )
        )
        stats = IndexStats.model_validate(index_raw)
        assert stats.fetched >= 3, f"expected several pages, got {stats.fetched!r}"
        assert normalize_doc_root(stats.source_base) == expected_root
        assert str(db_path) == stats.db_path

        sources = SourceListResponse.model_validate(
            _tool_structured(await m.call_tool("list_indexed_sources", {}))
        )
        assert str(db_path) == sources.db_path
        bases = {s.source_base for s in sources.sources}
        assert expected_root in bases
        src = next(s for s in sources.sources if s.source_base == expected_root)
        assert src.page_count >= 3

        res_parts = await m.read_resource("readdocs://status")
        status_text = next(iter(res_parts)).content
        status = json.loads(status_text)
        assert status["db_path"] == str(db_path)
        assert any(
            expected_root == x.get("source_base") for x in status.get("sources", [])
        )

        pages = ListPagesResponse.model_validate(
            _tool_structured(
                await m.call_tool(
                    "list_documentation_pages",
                    {"source_base": expected_root, "limit": 15, "offset": 0},
                )
            )
        )
        assert pages.total >= 1
        assert len(pages.pages) >= 1
        sample_url = pages.pages[0].url
        assert sample_url.startswith(expected_root)

        global_search = SearchResponse.model_validate(
            _tool_structured(
                await m.call_tool(
                    "search",
                    {
                        "query": "Intents gateway",
                        "limit": 8,
                        "source_base": expected_root,
                    },
                )
            )
        )
        assert global_search.results, "global FTS search returned no hits"
        hit = global_search.results[0]
        page_url = hit.url or hit.id

        in_file = SearchResponse.model_validate(
            _tool_structured(
                await m.call_tool(
                    "search_in_file",
                    {"page_url": page_url, "query": "discord", "limit": 10},
                )
            )
        )
        assert in_file.results, "search_in_file returned no hits"

        fetch_full = FetchResponse.model_validate(
            _tool_structured(await m.call_tool("fetch", {"id": page_url}))
        )
        assert fetch_full.text.strip()
        assert fetch_full.total_lines and fetch_full.total_lines > 0

        first_chunk = in_file.results[0]
        cs, ce = first_chunk.chunk_line_start, first_chunk.chunk_line_end
        if cs is not None and ce is not None and ce >= cs:
            sliced = FetchResponse.model_validate(
                _tool_structured(
                    await m.call_tool("fetch", {"id": page_url, "start": cs, "end": ce})
                )
            )
            assert sliced.slice_start is not None and sliced.slice_end is not None

        entities = EntitySearchResponse.model_validate(
            _tool_structured(
                await m.call_tool(
                    "search_entities",
                    {
                        "query": "Client",
                        "kind": "class",
                        "source_base": expected_root,
                        "limit": 10,
                        "include_related": True,
                    },
                )
            )
        )
        assert entities.results, (
            "search_entities found no classes (unexpected for discord.py)"
        )

        detail = EntityDetailResponse.model_validate(
            _tool_structured(
                await m.call_tool(
                    "get_entity",
                    {
                        "entity_id": entities.results[0].entity_id,
                        "include_methods": True,
                        "include_params": True,
                        "include_notes": True,
                    },
                )
            )
        )
        assert detail.entity is not None
        assert detail.entity.name

        methods = EntityMethodListResponse.model_validate(
            _tool_structured(
                await m.call_tool(
                    "list_class_methods",
                    {"class_entity_id": entities.results[0].entity_id},
                )
            )
        )
        assert isinstance(methods.methods, list)

        ctx = EntityDetailResponse.model_validate(
            _tool_structured(
                await m.call_tool(
                    "get_entity_context",
                    {"query": "discord.Client", "source_base": expected_root},
                )
            )
        )
        assert ctx.entity is not None

        sym = LookupSymbolResponse.model_validate(
            _tool_structured(
                await m.call_tool(
                    "lookup_symbol",
                    {"source_base": expected_root, "symbol_name": "discord.Client"},
                )
            )
        )
        assert sym.result.found is True
        assert sym.result.page_url

        related = RelatedSymbolsResponse.model_validate(
            _tool_structured(
                await m.call_tool(
                    "related_symbols",
                    {
                        "source_base": expected_root,
                        "symbol_name": "discord.Client",
                        "direction": "both",
                        "limit": 20,
                    },
                )
            )
        )
        assert related.found is True

        graph = SymbolGraphStatsResponse.model_validate(
            _tool_structured(
                await m.call_tool(
                    "get_symbol_graph_stats", {"source_base": expected_root}
                )
            )
        )
        assert graph.source_base == expected_root
        assert graph.total_pages >= 3
        assert graph.total_entities >= 1

        p_index = await m.get_prompt("index-docs", {"seed_url": SEED_URL})
        assert "index_readthedocs" in p_index.messages[0].content.text

        p_search = await m.get_prompt("search-docs", {"query": "Intents"})
        assert "search" in p_search.messages[0].content.text

        p_read = await m.get_prompt(
            "read-page", {"id": page_url, "question": "What is on this page?"}
        )
        assert page_url in p_read.messages[0].content.text

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_run())
