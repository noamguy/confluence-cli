"""Tests for the MCP-mode agent.

We patch both ``anthropic.Anthropic`` (to stub the streaming API) and the
agent's own MCP helpers so no real network connection to
``mcp.atlassian.com`` is ever attempted.
"""

from __future__ import annotations

import copy
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.mcp_agent import (
    McpAgent,
    _format_exception,
    _is_transient_mcp_error,
    _mcp_tool_to_anthropic,
)
from agent.oauth import TokenBundle


# ---------------------------------------------------------------------------
# Stub helpers (mirrors the REST-agent test helpers)
# ---------------------------------------------------------------------------


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(block_id: str, name: str, input_: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=input_)


def _message(content, stop_reason: str, input_tokens=10, output_tokens=5) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class _FakeStream:
    """Context manager mimicking ``anthropic.Messages.stream``."""

    def __init__(self, final_message: SimpleNamespace, text_chunks=()) -> None:
        self._final = final_message
        self._chunks = list(text_chunks)

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    @property
    def text_stream(self):
        return iter(self._chunks)

    def get_final_message(self) -> SimpleNamespace:
        return self._final


def _stream_driver(turns):
    """Build a ``messages.stream`` side_effect that replays fixed turns."""
    snapshots: list[list] = []
    turns_iter = iter(turns)

    def _stream(**kwargs):
        snapshots.append(copy.deepcopy(kwargs["messages"]))
        final_message, chunks = next(turns_iter)
        return _FakeStream(final_message, chunks)

    return _stream, snapshots


def _valid_token() -> TokenBundle:
    return TokenBundle(
        access_token="tok",
        refresh_token="refresh",
        expires_at=time.time() + 3600,
        cloud_id="cid",
        site_url="https://your-workspace.atlassian.net",
    )


# ---------------------------------------------------------------------------
# _mcp_tool_to_anthropic
# ---------------------------------------------------------------------------


def test_mcp_tool_to_anthropic_renames_schema_field() -> None:
    mcp_tool = SimpleNamespace(
        name="search",
        description="  Search confluence  ",
        inputSchema={"type": "object", "properties": {"q": {"type": "string"}}},
    )
    schema = _mcp_tool_to_anthropic(mcp_tool)
    assert schema == {
        "name": "search",
        "description": "Search confluence",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }


def test_mcp_tool_to_anthropic_defaults_empty_schema() -> None:
    mcp_tool = SimpleNamespace(name="noop", description=None, inputSchema=None)
    schema = _mcp_tool_to_anthropic(mcp_tool)
    assert schema["name"] == "noop"
    assert schema["description"] == ""
    assert schema["input_schema"]["type"] == "object"


# ---------------------------------------------------------------------------
# _format_exception — unwraps ExceptionGroup
# ---------------------------------------------------------------------------


def test_format_exception_unwraps_exception_group() -> None:
    inner = RuntimeError("real cause")
    group = ExceptionGroup("unhandled errors in a TaskGroup", [inner])
    formatted = _format_exception(group)
    assert "ExceptionGroup" in formatted
    assert "RuntimeError: real cause" in formatted


def test_format_exception_unwraps_nested_groups() -> None:
    deepest = ConnectionError("boom")
    nested = ExceptionGroup("inner", [deepest])
    outer = ExceptionGroup("outer", [nested])
    formatted = _format_exception(outer)
    # Each layer shows up indented further than the previous one.
    assert "ExceptionGroup: outer" in formatted
    assert "ExceptionGroup: inner" in formatted
    assert "ConnectionError: boom" in formatted


# ---------------------------------------------------------------------------
# _is_transient_mcp_error
# ---------------------------------------------------------------------------


def test_is_transient_mcp_error_matches_server_phrases() -> None:
    exc = RuntimeError(
        "MCP server returned isError=true for tool 'x'. Server response: "
        '{"error":true,"message":"We are having trouble completing this action. '
        'Please try again shortly."}'
    )
    assert _is_transient_mcp_error(exc) is True


def test_is_transient_mcp_error_rejects_permanent_errors() -> None:
    assert _is_transient_mcp_error(RuntimeError("401 Unauthorized")) is False
    assert _is_transient_mcp_error(RuntimeError("tool 'nope' not found")) is False


# ---------------------------------------------------------------------------
# McpAgent.ask
# ---------------------------------------------------------------------------


def test_ask_returns_answer_without_tool_use() -> None:
    oauth_client = MagicMock()

    final = _message(
        [_text_block("hello from mcp")],
        stop_reason="end_turn",
        input_tokens=50,
        output_tokens=5,
    )
    stream_fn, _ = _stream_driver([(final, ["hello ", "from mcp"])])

    fake_anthropic = MagicMock()
    fake_anthropic.messages.stream.side_effect = stream_fn

    with patch("agent.mcp_agent.anthropic.Anthropic", return_value=fake_anthropic), patch.object(
        McpAgent,
        "_ensure_tool_schemas",
        return_value=[{"name": "x", "description": "x", "input_schema": {"type": "object"}}],
    ):
        agent = McpAgent(
            anthropic_api_key="k",
            oauth_client=oauth_client,
            space_key="PH",
            token_bundle=_valid_token(),
        )
        emitted: list[str] = []
        response = agent.ask("hi", on_text=emitted.append)

    assert response.answer == "hello from mcp"
    assert emitted == ["hello ", "from mcp"]
    assert response.tool_calls == []
    assert response.usage == {"input": 50, "output": 5, "total": 55}


def test_ask_dispatches_mcp_tool_call() -> None:
    oauth_client = MagicMock()

    first = _message(
        [_tool_use_block("tu_1", "atlassian_search", {"q": "runbook"})],
        stop_reason="tool_use",
        input_tokens=24000,  # simulate the big schema injection
        output_tokens=30,
    )
    second = _message(
        [_text_block("done")],
        stop_reason="end_turn",
        input_tokens=24100,
        output_tokens=10,
    )

    stream_fn, _ = _stream_driver([(first, []), (second, ["done"])])

    fake_anthropic = MagicMock()
    fake_anthropic.messages.stream.side_effect = stream_fn

    async def fake_call_mcp_tool(self, name, arguments):
        assert name == "atlassian_search"
        assert arguments == {"q": "runbook"}
        return "TOOL RESULT"

    with patch("agent.mcp_agent.anthropic.Anthropic", return_value=fake_anthropic), patch.object(
        McpAgent,
        "_ensure_tool_schemas",
        return_value=[
            {"name": "atlassian_search", "description": "s", "input_schema": {"type": "object"}}
        ],
    ), patch.object(McpAgent, "_call_mcp_tool", new=fake_call_mcp_tool):
        agent = McpAgent(
            anthropic_api_key="k",
            oauth_client=oauth_client,
            space_key="PH",
            token_bundle=_valid_token(),
        )
        response = agent.ask("find the runbook")

    assert response.answer == "done"
    assert response.tool_calls == ['atlassian_search(q="runbook")']
    # Token totals include the deliberately-large input counts.
    assert response.usage["input"] == 24000 + 24100
    assert response.usage["total"] == response.usage["input"] + response.usage["output"]


def test_ask_handles_mcp_tool_error() -> None:
    oauth_client = MagicMock()

    first = _message(
        [_tool_use_block("tu_1", "atlassian_search", {"q": "x"})],
        stop_reason="tool_use",
    )
    second = _message([_text_block("recovered")], stop_reason="end_turn")

    stream_fn, snapshots = _stream_driver(
        [
            (first, []),
            (second, ["recovered"]),
        ]
    )

    fake_anthropic = MagicMock()
    fake_anthropic.messages.stream.side_effect = stream_fn

    async def fake_call_mcp_tool(self, name, arguments):
        raise RuntimeError("invalid_token")

    with patch("agent.mcp_agent.anthropic.Anthropic", return_value=fake_anthropic), patch.object(
        McpAgent,
        "_ensure_tool_schemas",
        return_value=[
            {"name": "atlassian_search", "description": "s", "input_schema": {"type": "object"}}
        ],
    ), patch.object(McpAgent, "_call_mcp_tool", new=fake_call_mcp_tool):
        agent = McpAgent(
            anthropic_api_key="k",
            oauth_client=oauth_client,
            space_key="PH",
            token_bundle=_valid_token(),
        )
        response = agent.ask("anything")

    tool_result_msg = snapshots[1][-1]
    assert tool_result_msg["content"][0]["is_error"] is True
    assert "invalid_token" in tool_result_msg["content"][0]["content"]
    assert response.answer == "recovered"
