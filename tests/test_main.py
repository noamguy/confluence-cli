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


# ---------------------------------------------------------------------------
# _render_error — mode-aware rate-limit guidance
# ---------------------------------------------------------------------------


class _FakeRateLimit(Exception):
    """Stand-in for ``anthropic.RateLimitError`` with just the bits we need."""

    status_code = 429

    def __init__(self, message: str = "rate_limit_error") -> None:
        super().__init__(message)


def test_render_error_points_mcp_rate_limit_to_rest_mode(capsys) -> None:
    """In MCP mode, a 429 must tell the user to switch to REST mode.

    This is the core educational moment of the whole project: the user
    has hit the exact failure the comparison table predicts, and we
    should make the fix glaringly obvious rather than dumping the raw
    SDK error.
    """
    exc = _FakeRateLimit(
        "Error code: 429 - {'type': 'error', 'error': "
        "{'type': 'rate_limit_error', 'message': "
        "\"10,000 input tokens per minute\"}}"
    )
    main._render_error(exc, mode="mcp")
    captured = capsys.readouterr().out
    assert "rate limit" in captured.lower()
    assert "rest" in captured.lower()
    # The exact fix command should be prominent.
    assert "python main.py --mode rest" in captured


def test_render_error_rest_rate_limit_suggests_waiting(capsys) -> None:
    """In REST mode, a 429 should suggest waiting (not switching mode)."""
    exc = _FakeRateLimit("rate_limit_error")
    main._render_error(exc, mode="rest")
    captured = capsys.readouterr().out
    assert "rate limit" in captured.lower()
    # Should NOT recommend switching modes from REST.
    assert "--mode rest" not in captured
    assert "60s" in captured or "refill" in captured.lower() or "wait" in captured.lower()


def test_render_error_falls_back_to_generic_for_unknown(capsys) -> None:
    """Non-rate-limit, non-overloaded errors render generically."""
    main._render_error(RuntimeError("something weird"), mode="rest")
    captured = capsys.readouterr().out
    assert "RuntimeError" in captured
    assert "something weird" in captured
