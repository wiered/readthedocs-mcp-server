"""Tests for the manual live CLI parser."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_live_cli_module():
    path = Path(__file__).with_name("live_cli_test.py")
    spec = importlib.util.spec_from_file_location("live_cli_test", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_split_repl_line_strips_quotes_from_source_url() -> None:
    live_cli = _load_live_cli_module()

    assert live_cli._split_repl_line(
        'search send_photo --source "https://pytba.readthedocs.io/en/latest/" 10'
    ) == [
        "search",
        "send_photo",
        "--source",
        "https://pytba.readthedocs.io/en/latest/",
        "10",
    ]
    assert live_cli._split_repl_line(
        'search send_photo 10 --source "https://pytba.readthedocs.io/en/latest/"'
    ) == [
        "search",
        "send_photo",
        "10",
        "--source",
        "https://pytba.readthedocs.io/en/latest/",
    ]


def test_split_repl_line_strips_quotes_from_grep_url() -> None:
    live_cli = _load_live_cli_module()

    assert live_cli._split_repl_line(
        'grep "https://pytba.readthedocs.io/en/latest/types.html" TeleBot 5'
    ) == [
        "grep",
        "https://pytba.readthedocs.io/en/latest/types.html",
        "TeleBot",
        "5",
    ]
