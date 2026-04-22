"""Debuggable live indexing runner.

Run from the repository root:

    python tests/live_test_debug.py

Useful variants:

    python tests/live_test_debug.py --breakpoint-before-index
    python tests/live_test_debug.py --debugpy --wait-for-debugger

This is intentionally not a pytest test. It is a normal script for attaching a
debugger to the indexing path when a live crawl appears to hang.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from readdocsserver.crawl import normalize_doc_root  # noqa: E402
from readdocsserver.main import create_server  # noqa: E402
from readdocsserver.services import indexing  # noqa: E402
from readdocsserver.services.deps import get_doc_index  # noqa: E402


DEFAULT_SEED_URL = "https://docs.readthedocs.com/platform/stable/index.html"
DEFAULT_SEED_URL = "https://discordpy.readthedocs.io/en/stable/api.html"


def get_doc_name_from_url(url: str) -> str:
    """
    Extracts the doc name from a URL like 'https://docs.readthedocs.com/platform/stable/index.html'
    and returns 'docs.readthedocs.com'.

    Examples:
        get_doc_name_from_url("https://docs.readthedocs.com/platform/stable/index.html")
        -> "docs.readthedocs.com"
    """
    import re

    m = re.match(r"^(?:\w+://)?([^/]+)", url)
    if m:
        return m.group(1)
    return ""


DOC_NAME = get_doc_name_from_url(DEFAULT_SEED_URL)
DEFAULT_DB_PATH = Path(tempfile.gettempdir()) / "readdocs-mcp-live-debug.sqlite"


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def _tool_structured(result: Any) -> dict[str, Any]:
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        return result[1]
    raise TypeError(f"Unexpected tool result shape: {result!r}")


def _configure_debugpy(host: str, port: int, wait: bool) -> None:
    try:
        import debugpy  # type: ignore[import-not-found]
    except ImportError as e:
        raise SystemExit(
            "debugpy is not installed. Install it or run without --debugpy."
        ) from e

    debugpy.listen((host, port))
    print(f"[debug] debugpy listening on {host}:{port}", flush=True)
    if wait:
        print("[debug] waiting for debugger attach...", flush=True)
        debugpy.wait_for_client()
        print("[debug] debugger attached", flush=True)


def _print_index_state(source_base: str, page_limit: int) -> None:
    idx = get_doc_index()
    sources = idx.list_sources()
    pages, total = idx.list_pages(source_base=source_base, limit=page_limit, offset=0)
    print("[debug] indexed sources:", flush=True)
    print(_json({"db_path": str(idx.db_path), "sources": sources}), flush=True)
    print("[debug] first indexed pages:", flush=True)
    print(_json({"total": total, "pages": pages}), flush=True)


async def _run_index_via_tool(args: argparse.Namespace) -> dict[str, Any]:
    m = create_server()
    from viztracer import VizTracer

    from datetime import datetime

    timestr = datetime.now().strftime("%Y%m%d_%H%M%S")
    doc_name = (
        get_doc_name_from_url(args.seed_url) if hasattr(args, "seed_url") else "unknown"
    )
    trace_dir = Path(__file__).parent.parent / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_filename = str(trace_dir / f"call_tool_trace_{doc_name}_{timestr}.json")
    tracer = VizTracer(output_file=trace_filename)
    tracer.start()
    try:
        result = await m.call_tool(
            "index_readthedocs",
            {
                "seed_url": args.seed_url,
                "max_pages": args.max_pages,
                "request_delay_sec": args.delay,
                "replace_source": args.replace_source,
            },
        )

        return _tool_structured(result)
    finally:
        tracer.stop()
        tracer.save()


async def _run_index_direct(args: argparse.Namespace) -> dict[str, Any]:
    stats = await indexing.run_index_readthedocs(
        args.seed_url,
        max_pages=args.max_pages,
        request_delay_sec=args.delay,
        replace_source=args.replace_source,
    )
    return stats.model_dump(mode="json")


async def _run(args: argparse.Namespace) -> int:
    source_base = normalize_doc_root(args.seed_url)
    started_at = time.monotonic()

    print("[debug] configuration:", flush=True)
    print(
        _json(
            {
                "mode": args.mode,
                "seed_url": args.seed_url,
                "source_base": source_base,
                "max_pages": args.max_pages,
                "delay": args.delay,
                "replace_source": args.replace_source,
                "db_path": os.environ["READTHEDOCS_MCP_DB"],
            }
        ),
        flush=True,
    )

    if args.breakpoint_before_index:
        breakpoint()

    if args.mode == "tool":
        stats = await _run_index_via_tool(args)
    else:
        stats = await _run_index_direct(args)

    elapsed = time.monotonic() - started_at
    print(f"[debug] indexing finished in {elapsed:.1f}s", flush=True)
    print("[debug] index stats:", flush=True)
    print(_json(stats), flush=True)

    fetched = int(stats.get("fetched") or 0)
    if fetched < args.min_fetched:
        print(
            f"[debug] FAIL: expected at least {args.min_fetched} fetched pages, got {fetched}",
            file=sys.stderr,
            flush=True,
        )
        _print_index_state(source_base, args.page_preview_limit)
        return 1

    _print_index_state(source_base, args.page_preview_limit)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run live Read the Docs indexing as a normal debuggable script.",
    )
    parser.add_argument(
        "seed_url",
        nargs="?",
        default=os.environ.get("READTHEDOCS_LIVE_SEED_URL", DEFAULT_SEED_URL),
        help=f"Read the Docs / Sphinx seed URL. Default: {DEFAULT_SEED_URL}",
    )
    parser.add_argument(
        "--mode",
        choices=("tool", "direct"),
        default="tool",
        help="tool calls the FastMCP tool; direct calls the indexing service.",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("READTHEDOCS_MCP_DB", str(DEFAULT_DB_PATH)),
        help="SQLite path. Default: temp readdocs-mcp-live-debug.sqlite.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=int(os.environ.get("READTHEDOCS_LIVE_MAX_PAGES", "20")),
        help="Maximum pages to fetch.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=float(os.environ.get("READTHEDOCS_LIVE_DELAY_SEC", "0.05")),
        help="Delay between HTTP requests.",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Do not clear this source before indexing.",
    )
    parser.add_argument(
        "--min-fetched",
        type=int,
        default=1,
        help="Exit with code 1 if fewer pages are fetched.",
    )
    parser.add_argument(
        "--page-preview-limit",
        type=int,
        default=10,
        help="How many indexed pages to print after the run.",
    )
    parser.add_argument(
        "--breakpoint-before-index",
        action="store_true",
        help="Call built-in breakpoint() immediately before indexing starts.",
    )
    parser.add_argument(
        "--debugpy",
        action="store_true",
        help="Open a debugpy listener before indexing.",
    )
    parser.add_argument(
        "--debugpy-host",
        default="127.0.0.1",
        help="debugpy listen host.",
    )
    parser.add_argument(
        "--debugpy-port",
        type=int,
        default=5678,
        help="debugpy listen port.",
    )
    parser.add_argument(
        "--wait-for-debugger",
        action="store_true",
        help="Wait until a debugpy client attaches.",
    )
    args = parser.parse_args()
    args.replace_source = not args.append
    return args


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    args = _parse_args()
    os.environ["READTHEDOCS_MCP_DB"] = str(Path(args.db).expanduser())

    if args.debugpy:
        _configure_debugpy(args.debugpy_host, args.debugpy_port, args.wait_for_debugger)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
