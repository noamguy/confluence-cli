"""Tests for CLI-level helpers in main.py.

Focused on small, pure functions that don't require standing up a full
agent or network stack — right now just the ``--reset`` behaviour and
the argparse wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import main


def test_reset_cached_token_removes_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text('{"access_token": "x"}')
    monkeypatch.setenv("CONFLUENCE_CLI_TOKEN_PATH", str(token_path))

    main.reset_cached_token()

    assert not token_path.exists()


def test_reset_cached_token_is_noop_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "does-not-exist.json"
    monkeypatch.setenv("CONFLUENCE_CLI_TOKEN_PATH", str(token_path))

    # Should not raise.
    main.reset_cached_token()

    assert not token_path.exists()


def test_arg_parser_accepts_reset_flag() -> None:
    parser = main.build_arg_parser()
    args = parser.parse_args(["--reset"])
    assert args.reset is True
    assert args.mode == "rest"  # default preserved


def test_arg_parser_reset_defaults_false() -> None:
    parser = main.build_arg_parser()
    args = parser.parse_args([])
    assert args.reset is False
