"""Интерактивная «песочница» для ручной проверки MCP-инструментов без Cursor.

Запуск из корня репозитория (нужен PYTHONPATH=src, как у pytest):

    .venv\\Scripts\\python.exe tests/live_cli_test.py

Опционально свой SQLite:

    set READTHEDOCS_MCP_DB=%TEMP%\\readdocs-play.sqlite
    .venv\\Scripts\\python.exe tests/live_cli_test.py

В консоли вводите команды (см. help). Это не автотесты: pytest сюда тесты не подхватывает.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

# Репозиторий: пакет в src/ (как в pyproject pytest pythonpath)
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from readdocsserver.main import create_server  # noqa: E402


_MAX_PREVIEW_CHARS = 12_000


def _split_repl_line(raw: str) -> list[str]:
    # Use POSIX shell parsing on every platform so quotes group arguments
    # without becoming part of URLs passed to MCP tools.
    return shlex.split(raw, posix=True)


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _format_tool_result(result: Any) -> None:
    """FastMCP.call_tool при structured output даёт (content_blocks, structured_dict)."""
    if isinstance(result, tuple) and len(result) == 2:
        _blocks, structured = result
        if isinstance(structured, dict):
            text_keys = ("text", "body", "content")
            for key in text_keys:
                if (
                    key in structured
                    and isinstance(structured[key], str)
                    and len(structured[key]) > _MAX_PREVIEW_CHARS
                ):
                    copy = {
                        **structured,
                        key: structured[key][:_MAX_PREVIEW_CHARS]
                        + "\n… [truncated for console]",
                    }
                    _print_json(copy)
                    return
            _print_json(structured)
            return
    _print_json(result) if isinstance(result, dict) else print(result)


async def _cmd_tools(m: Any) -> None:
    tools = await m.list_tools()
    for t in tools:
        desc = (t.description or "").strip().replace("\n", " ")
        if len(desc) > 120:
            desc = desc[:117] + "..."
        print(f"- {t.name}: {desc}")


async def _cmd_prompts(m: Any) -> None:
    prompts = await m.list_prompts()
    for p in prompts:
        desc = (p.description or "").strip().replace("\n", " ")
        if len(desc) > 120:
            desc = desc[:117] + "..."
        print(f"- {p.name}: {desc}")


async def _cmd_resources(m: Any) -> None:
    resources = await m.list_resources()
    for r in resources:
        desc = (r.description or "").strip().replace("\n", " ")
        if len(desc) > 120:
            desc = desc[:117] + "..."
        print(f"- {r.uri}: {desc}")


async def _cmd_status(m: Any) -> None:
    parts = await m.read_resource("readdocs://status")
    first = next(iter(parts))
    print(first.content)


async def _cmd_prompt(m: Any, name: str, arguments: dict[str, Any]) -> None:
    out = await m.get_prompt(name, arguments)
    _print_json(out.model_dump(mode="json"))


async def _run_one(m: Any, name: str, arguments: dict[str, Any]) -> None:
    out = await m.call_tool(name, arguments)
    _format_tool_result(out)


def _usage() -> None:
    print(
        """
Команды (одна строка, аргументы как в shell — URL в кавычках при необходимости):

  help              эта справка
  tools             список имён MCP tools
  prompts           список MCP prompts
  resources         список MCP resources
  db                показать READTHEDOCS_MCP_DB (если задан)
  list              list_indexed_sources
  sources           alias для list
  pages [опции]     list_documentation_pages
  status            ресурс readdocs://status (JSON)
  search QUERY [N] [--source ROOT]
                    search(query, limit=N, source_base=ROOT)
  grep URL QUERY [N]
                    search_in_file(page_url=URL, query=QUERY, limit=N)
  fetch URL [a b]   fetch(id=URL); a,b — номера строк 1-based inclusive, оба или ни одного
  index URL [опции]
  pindex URL        prompt index-docs(seed_url=URL)
  psearch QUERY     prompt search-docs(query=QUERY)
  pread URL [TEXT]  prompt read-page(id=URL, question=TEXT)

Опции pages:
  --source ROOT     source_base
  --url SUBSTR      url_contains
  --limit N         по умолчанию 200
  --offset N        по умолчанию 0

Опции index (в конце строки):
  --max-pages N     по умолчанию 200
  --delay SEC       request_delay_sec, по умолчанию 0.15
  --append          не очищать индекс для этого source (replace_source=False)

Примеры:
  search send_photo 10 --source "https://pytba.readthedocs.io/en/latest/"
  grep "https://pytba.readthedocs.io/en/latest/types.html" TeleBot 5
  pages --source "https://pytba.readthedocs.io/en/latest/" --limit 20
  fetch "https://pytba.readthedocs.io/en/latest/types.html" 1 80
  index "https://pytba.readthedocs.io/en/latest/" --max-pages 5 --delay 0

Для настоящего stdio/SSE MCP-сервера: READTHEDOCS_MCP_TRANSPORT=stdio и
  .venv\\Scripts\\python.exe -m readdocsserver
""".strip()
    )


async def _repl() -> None:
    m = create_server()
    db = os.environ.get("READTHEDOCS_MCP_DB")
    print("readdocs MCP playground (локальный вызов FastMCP.call_tool). Введите help.")
    print(
        f"READTHEDOCS_MCP_DB={db!r}"
        if db
        else "READTHEDOCS_MCP_DB not set (default ~/.cache/readdocs-mcp/index.sqlite)"
    )

    while True:
        try:
            line = await asyncio.to_thread(input, "mcp> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        raw = line.strip().lstrip("\ufeff")
        if not raw:
            continue
        try:
            parts = _split_repl_line(raw)
        except ValueError as e:
            print(f"parse error: {e}")
            continue

        if not parts:
            continue

        t0 = parts[0].lstrip("\ufeff")
        while t0 and not (t0[0].isascii() and (t0[0].isalnum() or t0[0] in "-_")):
            t0 = t0[1:]
        parts[0] = t0
        if not parts[0]:
            continue

        cmd = parts[0].lower()
        args = parts[1:]

        try:
            if cmd in {"q", "quit", "exit"}:
                break
            if cmd in {"h", "help", "?"}:
                _usage()
            elif cmd == "tools":
                await _cmd_tools(m)
            elif cmd == "prompts":
                await _cmd_prompts(m)
            elif cmd == "resources":
                await _cmd_resources(m)
            elif cmd == "db":
                print(os.environ.get("READTHEDOCS_MCP_DB", "(not set)"))
            elif cmd in {"list", "sources"}:
                await _run_one(m, "list_indexed_sources", {})
            elif cmd == "status":
                await _cmd_status(m)
            elif cmd == "pages":
                source_base = None
                url_contains = None
                limit = 200
                offset = 0
                i = 0
                bad_usage = False
                while i < len(args):
                    a = args[i]
                    if a == "--source" and i + 1 < len(args):
                        source_base = args[i + 1]
                        i += 2
                        continue
                    if a == "--url" and i + 1 < len(args):
                        url_contains = args[i + 1]
                        i += 2
                        continue
                    if a == "--limit" and i + 1 < len(args):
                        limit = int(args[i + 1])
                        i += 2
                        continue
                    if a == "--offset" and i + 1 < len(args):
                        offset = int(args[i + 1])
                        i += 2
                        continue
                    print(
                        "usage: pages [--source ROOT] [--url SUBSTR] "
                        "[--limit N] [--offset N]"
                    )
                    bad_usage = True
                    break
                if bad_usage:
                    continue
                payload: dict[str, Any] = {"limit": limit, "offset": offset}
                if source_base:
                    payload["source_base"] = source_base
                if url_contains:
                    payload["url_contains"] = url_contains
                await _run_one(m, "list_documentation_pages", payload)
            elif cmd == "search":
                if not args:
                    print("usage: search QUERY [limit] [--source ROOT]")
                    continue
                limit = 15
                source_base = None
                rest: list[str] = []
                i = 0
                while i < len(args):
                    a = args[i]
                    if a == "--source" and i + 1 < len(args):
                        source_base = args[i + 1]
                        i += 2
                        continue
                    rest.append(a)
                    i += 1
                # Limit is the last token only if at least one query token remains
                # (so "search 404" searches for 404, not "empty query").
                if len(rest) >= 2 and rest[-1].isdigit():
                    limit = int(rest.pop())
                query = " ".join(rest).strip()
                if not query:
                    print("empty query")
                    continue
                payload: dict[str, Any] = {"query": query, "limit": limit}
                if source_base:
                    payload["source_base"] = source_base
                await _run_one(m, "search", payload)
            elif cmd == "grep":
                if len(args) < 2:
                    print("usage: grep URL QUERY [limit]")
                    continue
                limit = 15
                if args[-1].isdigit():
                    limit = int(args[-1])
                    q_parts = args[1:-1]
                else:
                    q_parts = args[1:]
                query = " ".join(q_parts).strip()
                if not query:
                    print("empty query")
                    continue
                await _run_one(
                    m,
                    "search_in_file",
                    {"page_url": args[0], "query": query, "limit": limit},
                )
            elif cmd == "fetch":
                if not args:
                    print("usage: fetch URL [start end]")
                    continue
                if len(args) >= 3 and args[-2].isdigit() and args[-1].isdigit():
                    url, a, b = args[0], int(args[-2]), int(args[-1])
                    await _run_one(m, "fetch", {"id": url, "start": a, "end": b})
                else:
                    await _run_one(m, "fetch", {"id": args[0]})
            elif cmd == "index":
                if not args:
                    print("usage: index URL [--max-pages N] [--delay SEC] [--append]")
                    continue
                max_pages = 200
                delay = 0.15
                replace_source = True
                url_tokens: list[str] = []
                i = 0
                while i < len(args):
                    a = args[i]
                    if a == "--max-pages" and i + 1 < len(args):
                        max_pages = int(args[i + 1])
                        i += 2
                        continue
                    if a == "--delay" and i + 1 < len(args):
                        delay = float(args[i + 1])
                        i += 2
                        continue
                    if a == "--append":
                        replace_source = False
                        i += 1
                        continue
                    url_tokens.append(a)
                    i += 1
                seed_url = " ".join(url_tokens).strip()
                if not seed_url:
                    print("missing URL")
                    continue
                await _run_one(
                    m,
                    "index_readthedocs",
                    {
                        "seed_url": seed_url,
                        "max_pages": max_pages,
                        "request_delay_sec": delay,
                        "replace_source": replace_source,
                    },
                )
            elif cmd == "pindex":
                if len(args) != 1:
                    print("usage: pindex URL")
                    continue
                await _cmd_prompt(m, "index-docs", {"seed_url": args[0]})
            elif cmd == "psearch":
                query = " ".join(args).strip()
                if not query:
                    print("usage: psearch QUERY")
                    continue
                await _cmd_prompt(m, "search-docs", {"query": query})
            elif cmd == "pread":
                if not args:
                    print("usage: pread URL [question]")
                    continue
                payload: dict[str, Any] = {"id": args[0]}
                question = " ".join(args[1:]).strip()
                if question:
                    payload["question"] = question
                await _cmd_prompt(m, "read-page", payload)
            else:
                print(f"unknown command: {cmd!r} (help)")
        except Exception as e:
            print(f"error: {e}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except OSError:
            pass
    asyncio.run(_repl())


if __name__ == "__main__":
    main()
