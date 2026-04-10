"""REST-mode agent: Claude + direct Confluence REST API.

This is the "control" implementation of the two modes. It uses the
Anthropic Messages API with tool use, where the tools are defined in
:mod:`agent.tools` and execute against Confluence's REST API directly
using the shared OAuth token from :mod:`agent.oauth`.

Compared with the MCP-backed agent, this mode:

* injects only *our* small tool schemas into each request (far fewer tokens);
* talks to a plain HTTP API we control and can debug;
* has no dependency on Atlassian's hosted MCP server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import anthropic

from .oauth import OAuthClient, TokenBundle
from .tools import TOOL_SCHEMAS, ConfluenceRestClient, execute_tool

#: Hard-coded per the project spec.
MODEL = "claude-sonnet-4-6"

#: Max tool-use / model iterations per user question. Prevents runaway loops.
MAX_ITERATIONS = 8

#: System prompt given to Claude. Kept short to keep input tokens low.
SYSTEM_PROMPT = (
    "You are a helpful assistant that answers questions about a Confluence "
    "workspace. You have tools to search and read Confluence pages. Use them "
    "as needed, then answer the user's question concisely and cite the page "
    "titles (and URLs if available) you used."
)


@dataclass
class AgentResponse:
    """Structured result returned by :meth:`RestAgent.ask`.

    Attributes:
        answer:     The final assistant text answer.
        tool_calls: A list of ``"tool_name(arg=value, ...)"`` strings for
                    display in the ``[Tools]`` footer.
        usage:      A dict with ``input``, ``output``, and ``total`` token counts.
    """

    answer: str
    tool_calls: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0, "total": 0})


def _format_tool_call(name: str, arguments: dict) -> str:
    """Render a single tool call for the `[Tools]` footer.

    Produces output like ``confluence_search("payments outage")`` or
    ``get_page(id=589825)`` — one argument per call, keyword-formatted,
    quoted for non-numeric strings. Numeric-looking string ids are
    printed unquoted to match the project spec example.
    """
    # Single-positional shortcut for the common search case.
    if list(arguments.keys()) == ["query"]:
        return f'{name}("{arguments["query"]}")'

    def _render(value: object) -> str:
        if isinstance(value, str):
            if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
                return value
            return f'"{value}"'
        return str(value)

    parts = [f"{key}={_render(value)}" for key, value in arguments.items()]
    return f"{name}({', '.join(parts)})"


class RestAgent:
    """Agent that answers questions by calling Confluence's REST API.

    The agent is stateless across ``ask()`` calls by default (each call
    gets a fresh message history). The underlying OAuth token is refreshed
    transparently by :class:`OAuthClient` as needed.
    """

    def __init__(
        self,
        anthropic_api_key: str,
        oauth_client: OAuthClient,
        space_key: str,
        token_bundle: Optional[TokenBundle] = None,
        model: str = MODEL,
    ) -> None:
        """Create a new REST agent.

        Args:
            anthropic_api_key: API key for Claude.
            oauth_client:      Configured Atlassian OAuth client.
            space_key:         Confluence space key to search within.
            token_bundle:      Optional pre-fetched token bundle (mainly for tests).
            model:             Claude model id (defaults to claude-sonnet-4-6).
        """
        self.anthropic = anthropic.Anthropic(api_key=anthropic_api_key)
        self.oauth_client = oauth_client
        self.space_key = space_key
        self.model = model
        self._token = token_bundle or oauth_client.get_valid_token()
        self._rest = self._build_rest_client()

    def _build_rest_client(self) -> ConfluenceRestClient:
        """Construct a REST client bound to the current token."""
        return ConfluenceRestClient(
            access_token=self._token.access_token,
            cloud_id=self._token.cloud_id,
            space_key=self.space_key,
            site_url=self._token.site_url,
        )

    def _ensure_fresh_token(self) -> None:
        """Refresh the token (and rebuild the REST client) if it has expired."""
        if self._token.is_expired():
            self._token = self.oauth_client.get_valid_token()
            self._rest = self._build_rest_client()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ask(
        self,
        question: str,
        on_text: Optional[Callable[[str], None]] = None,
        on_tool_call: Optional[Callable[[str], None]] = None,
        on_turn_start: Optional[Callable[[], None]] = None,
    ) -> AgentResponse:
        """Answer a single natural-language question about Confluence.

        Runs the tool-use loop with **streaming**: send messages, if Claude
        returns ``tool_use`` blocks, execute them and feed results back,
        repeat until Claude returns a final text answer or we hit
        :data:`MAX_ITERATIONS`.

        Args:
            question:      The user's natural-language question.
            on_text:       Optional callback invoked with each streamed text
                           chunk as Claude produces it. The REPL uses this
                           to render a live Markdown view.
            on_tool_call:  Optional callback invoked once per tool-use block
                           with the formatted call string (e.g.
                           ``confluence_search("payments outage")``). Used
                           by the REPL to show a colored progress marker.
            on_turn_start: Optional callback invoked immediately before
                           each Claude roundtrip. Used by the REPL to
                           show a "thinking…" spinner between turns.
        """
        self._ensure_fresh_token()

        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        tool_calls_log: list[str] = []
        input_total = 0
        output_total = 0
        streamed_answer: list[str] = []

        for _ in range(MAX_ITERATIONS):
            if on_turn_start is not None:
                on_turn_start()

            with self.anthropic.messages.stream(
                model=self.model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                tools=TOOL_SCHEMAS,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    streamed_answer.append(text)
                    if on_text is not None:
                        on_text(text)
                final_message = stream.get_final_message()

            input_total += getattr(final_message.usage, "input_tokens", 0) or 0
            output_total += getattr(final_message.usage, "output_tokens", 0) or 0

            # Capture the assistant message (tool_use + any text) verbatim.
            messages.append({"role": "assistant", "content": final_message.content})

            if final_message.stop_reason != "tool_use":
                # Final answer already streamed; recover from content
                # blocks only if the stream never surfaced any text.
                answer = "".join(streamed_answer).strip()
                if not answer:
                    answer = "".join(
                        block.text
                        for block in final_message.content
                        if getattr(block, "type", "") == "text"
                    ).strip()
                return AgentResponse(
                    answer=answer,
                    tool_calls=tool_calls_log,
                    usage={
                        "input": input_total,
                        "output": output_total,
                        "total": input_total + output_total,
                    },
                )

            # Execute every tool_use block in this turn and build one
            # user message with all the tool_result blocks.
            tool_results: list[dict[str, Any]] = []
            for block in final_message.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                name = block.name
                arguments = block.input or {}
                call_str = _format_tool_call(name, arguments)
                tool_calls_log.append(call_str)
                if on_tool_call is not None:
                    on_tool_call(call_str)
                try:
                    result = execute_tool(self._rest, name, arguments)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": _stringify_tool_result(result),
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - surface errors to Claude
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "is_error": True,
                            "content": f"Tool error: {exc}",
                        }
                    )

            messages.append({"role": "user", "content": tool_results})

        # Iteration budget exhausted.
        return AgentResponse(
            answer="(Stopped: exceeded max tool-use iterations before a final answer.)",
            tool_calls=tool_calls_log,
            usage={
                "input": input_total,
                "output": output_total,
                "total": input_total + output_total,
            },
        )


def _stringify_tool_result(result: Any) -> str:
    """Serialize a tool result into a compact JSON string for Claude."""
    import json

    try:
        return json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(result)
