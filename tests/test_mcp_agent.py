"""Tests for the MCP-mode agent.

We patch both ``anthropic.Anthropic`` (to stub the streaming API) and
the agent's own MCP helpers so no real ``mcp-remote`` subprocess is
ever spawned and no network connection to ``mcp.atlassian.com`` is
ever attempted.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.mcp_agent import (
    MCP_REMOTE_ARGS,
    MCP_REMOTE_COMMAND,
    McpAgent,
    _format_exception,
    _is_transient_mcp_error,
    _mcp_tool_to_anthropic,
)

from tests.conftest import _message, _stream_driver, _text_block, _tool_use_block


def _build_agent() -> McpAgent:
    """Construct an McpAgent with a mocked Anthropic client.

    The MCP transport is stubbed out everywhere this helper is called,
    so no ``mcp-remote`` subprocess is ever spawned.
    """
    return McpAgent(anthropic_api_key="k", space_key="PH")


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
        agent = _build_agent()
        emitted: list[str] = []
        response = agent.ask("hi", on_text=emitted.append)

    assert response.answer == "hello from mcp"
    assert emitted == ["hello ", "from mcp"]
    assert response.tool_calls == []
    assert response.usage == {"input": 50, "output": 5, "total": 55}


def test_ask_dispatches_mcp_tool_call() -> None:
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
        agent = _build_agent()
        response = agent.ask("find the runbook")

    assert response.answer == "done"
    assert response.tool_calls == ['atlassian_search(q="runbook")']
    # Token totals include the deliberately-large input counts.
    assert response.usage["input"] == 24000 + 24100
    assert response.usage["total"] == response.usage["input"] + response.usage["output"]


def test_ask_handles_mcp_tool_error() -> None:
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
        agent = _build_agent()
        response = agent.ask("anything")

    tool_result_msg = snapshots[1][-1]
    assert tool_result_msg["content"][0]["is_error"] is True
    assert "invalid_token" in tool_result_msg["content"][0]["content"]
    assert response.answer == "recovered"


# ---------------------------------------------------------------------------
# Transport plumbing — mcp-remote stdio subprocess
# ---------------------------------------------------------------------------


def test_mcp_remote_command_is_npx_mcp_remote() -> None:
    """The stdio subprocess must be 'npx -y mcp-remote <url>'.

    Pinning this so a future refactor doesn't accidentally revert to
    the broken direct-HTTP-with-bearer-token approach.
    """
    assert MCP_REMOTE_COMMAND == "npx"
    assert MCP_REMOTE_ARGS[0] == "-y"
    assert MCP_REMOTE_ARGS[1] == "mcp-remote"
    assert MCP_REMOTE_ARGS[2].startswith("https://mcp.atlassian.com/")


def test_stdio_params_uses_mcp_remote_command() -> None:
    """McpAgent._stdio_params must target the mcp-remote proxy."""
    agent = _build_agent()
    params = agent._stdio_params()
    assert params.command == "npx"
    assert params.args == ["-y", "mcp-remote", agent.mcp_url]


def test_stdio_params_honours_mcp_url_override() -> None:
    """Overriding mcp_url propagates to the mcp-remote subprocess args."""
    agent = McpAgent(
        anthropic_api_key="k",
        space_key="PH",
        mcp_url="https://example.test/alt/mcp",
    )
    params = agent._stdio_params()
    assert params.args[-1] == "https://example.test/alt/mcp"


def test_mcp_agent_constructor_does_not_take_oauth_client() -> None:
    """MCP mode must not require an OAuth client — mcp-remote handles auth.

    This is a regression guard: the old API took an ``oauth_client``
    positional argument and used it to build an Authorization header,
    which Atlassian's MCP server silently rejected. The new API
    deliberately has no OAuth dependency at all.
    """
    import inspect

    sig = inspect.signature(McpAgent.__init__)
    assert "oauth_client" not in sig.parameters
    assert "token_bundle" not in sig.parameters
