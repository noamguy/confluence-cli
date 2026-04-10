"""MCP-mode agent: Claude + official Atlassian MCP server.

This mode delegates tool discovery and execution to the hosted Atlassian
MCP server at ``https://mcp.atlassian.com``. Instead of defining tools
ourselves, we:

    1. Connect to the MCP server over streamable HTTP, authenticating with
       the same Atlassian OAuth token used by the REST agent.
    2. Ask the server for its tool list and convert those MCP tool
       definitions into Anthropic tool schemas.
    3. Run the standard Claude tool-use loop, dispatching every tool call
       back through the MCP ``call_tool`` RPC.

Compared with the REST agent this implementation is shorter *in our own
code*, but every request pays the cost of Atlassian injecting its full
schema (~24k tokens) and every tool call is subject to the hosted server's
reliability. See the README comparison table for the trade-offs.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Callable, Optional

import anthropic

from .oauth import OAuthClient, TokenBundle
from .rest_agent import (
    AgentResponse,
    _format_tool_call,
    _stringify_tool_result,
)

#: Atlassian hosted MCP endpoint (streamable HTTP transport).
ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp"

#: Max attempts (including the first try) for a single MCP tool call
#: when the server returns a transient error.
MCP_MAX_ATTEMPTS = 3

#: Seconds to wait before the first retry; subsequent retries use
#: exponential backoff (1s, 2s, 4s, …).
MCP_RETRY_BASE_DELAY = 1.0

#: Substrings we treat as "transient" in an MCP server error message.
#: These come verbatim from Atlassian's MCP server responses.
_TRANSIENT_ERROR_MARKERS = (
    "having trouble completing this action",
    "try again shortly",
    "please try again",
)


def _is_transient_mcp_error(exc: BaseException) -> bool:
    """Return True if an MCP RuntimeError looks like a transient server issue.

    We match on the exact phrases the Atlassian MCP server uses in its
    ``{"error": true, "message": "..."}`` payload. Anything else — auth
    failures, schema mismatches, tool-not-found — is raised immediately
    without retrying, since retrying would just waste time.
    """
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_ERROR_MARKERS)


def _format_exception(exc: BaseException, depth: int = 0) -> str:
    """Recursively format an exception, unwrapping ``BaseExceptionGroup``.

    The ``mcp`` SDK runs its transport over ``anyio`` TaskGroups. When a
    sub-task fails, anyio wraps the real exception in a
    ``BaseExceptionGroup`` whose ``str()`` is just the useless summary
    ``"unhandled errors in a TaskGroup (1 sub-exception)"``. We walk the
    group so the caller sees the actual root cause (a ``ConnectError``,
    ``HTTPStatusError``, ``ValidationError``, …).
    """
    indent = "  " * depth
    header = f"{indent}{type(exc).__name__}: {exc}"
    if isinstance(exc, BaseExceptionGroup):
        lines = [header]
        for sub in exc.exceptions:
            lines.append(_format_exception(sub, depth + 1))
        return "\n".join(lines)
    # Chase __cause__ / __context__ so e.g. "RuntimeError: foo" caused by
    # "ConnectError: dns failure" prints both layers.
    chained = exc.__cause__ or (exc.__context__ if not exc.__suppress_context__ else None)
    if chained is not None:
        return header + "\n" + _format_exception(chained, depth + 1)
    return header

#: Same model as the REST agent, per project spec.
MODEL = "claude-sonnet-4-6"

#: Hard cap on tool-use iterations per question.
MAX_ITERATIONS = 8

SYSTEM_PROMPT = (
    "You are a helpful assistant that answers questions about a Confluence "
    "workspace using the Atlassian MCP tools available to you. Use the tools "
    "as needed, then answer the user's question concisely with citations."
)


class McpAgent:
    """Agent that answers questions via the official Atlassian MCP server.

    The MCP session is opened lazily on the first :meth:`ask` call and
    reused across subsequent calls in the same REPL loop. Token refresh is
    handled by :class:`OAuthClient`; if the access token expires we rebuild
    the MCP session transparently.
    """

    def __init__(
        self,
        anthropic_api_key: str,
        oauth_client: OAuthClient,
        space_key: str,
        token_bundle: Optional[TokenBundle] = None,
        model: str = MODEL,
        mcp_url: str = ATLASSIAN_MCP_URL,
    ) -> None:
        """Create a new MCP agent.

        Args:
            anthropic_api_key: API key for Claude.
            oauth_client:      Configured Atlassian OAuth client.
            space_key:         Confluence space key (passed to the model as context).
            token_bundle:      Optional pre-fetched token bundle (mainly for tests).
            model:             Claude model id.
            mcp_url:           Override of the Atlassian MCP endpoint.
        """
        self.anthropic = anthropic.Anthropic(api_key=anthropic_api_key)
        self.oauth_client = oauth_client
        self.space_key = space_key
        self.model = model
        self.mcp_url = mcp_url
        self._token = token_bundle or oauth_client.get_valid_token()
        # Cache of MCP tool definitions in Anthropic format.
        self._tool_schemas: Optional[list[dict[str, Any]]] = None

    # ------------------------------------------------------------------
    # MCP plumbing
    # ------------------------------------------------------------------

    async def _list_mcp_tools(self) -> list[dict[str, Any]]:
        """Connect to the MCP server and convert its tool list to Anthropic format.

        We keep the connection short-lived on purpose: every request opens,
        lists tools (or calls one), and closes. This mirrors what the
        hosted server expects and avoids holding an idle SSE connection —
        which is exactly the class of failure the ``invalid_token``
        degradation in the README comparison table refers to.
        """
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {self._token.access_token}"}
        async with streamablehttp_client(self.mcp_url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                return [_mcp_tool_to_anthropic(t) for t in listed.tools]

    async def _call_mcp_tool(self, name: str, arguments: dict) -> Any:
        """Invoke a single MCP tool and return its content payload.

        Implements a small retry loop for server-reported *transient*
        errors — the Atlassian MCP server periodically returns
        ``{"error": true, "message": "We are having trouble completing
        this action. Please try again shortly."}`` which is explicitly
        flagged as temporary by the server itself. We retry up to
        :data:`MCP_MAX_ATTEMPTS` times with exponential backoff before
        giving up; non-transient errors are raised on the first attempt.

        Raises:
            RuntimeError: when the MCP server returns a ``CallToolResult``
                with ``isError=True`` after all retries are exhausted, or
                immediately when the error is not recognised as transient.
        """
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        last_error: Optional[RuntimeError] = None

        for attempt in range(MCP_MAX_ATTEMPTS):
            try:
                headers = {"Authorization": f"Bearer {self._token.access_token}"}
                async with streamablehttp_client(self.mcp_url, headers=headers) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(name, arguments)

                        # MCP returns a content list; concatenate any text blocks.
                        text = ""
                        if result.content:
                            text = "\n".join(
                                getattr(c, "text", str(c)) for c in result.content
                            )

                        # A successful transport call can still carry an
                        # error payload. Without this check Claude sees
                        # the error text as if it were real data and
                        # paraphrases it back to the user as an apology,
                        # hiding the real cause.
                        if getattr(result, "isError", False):
                            raise RuntimeError(
                                f"MCP server returned isError=true for tool "
                                f"{name!r}. Server response: "
                                f"{text[:500] if text else '(empty)'}"
                            )
                        return text
            except RuntimeError as exc:
                last_error = exc
                if not _is_transient_mcp_error(exc) or attempt == MCP_MAX_ATTEMPTS - 1:
                    raise
                delay = MCP_RETRY_BASE_DELAY * (2 ** attempt)
                print(
                    f"  \u21bb MCP {name!r} returned a transient error; "
                    f"retrying in {delay:.0f}s "
                    f"(attempt {attempt + 2}/{MCP_MAX_ATTEMPTS})…",
                    file=sys.stderr,
                    flush=True,
                )
                await asyncio.sleep(delay)

        # Exhausted all attempts — re-raise the last error we saw.
        assert last_error is not None
        raise last_error

    def _ensure_tool_schemas(self) -> list[dict[str, Any]]:
        """Lazily fetch and cache the MCP tool schemas for Claude."""
        if self._tool_schemas is None:
            self._tool_schemas = asyncio.run(self._list_mcp_tools())
        return self._tool_schemas

    def _ensure_fresh_token(self) -> None:
        """Refresh the OAuth token if it has expired, invalidating the tool cache."""
        if self._token.is_expired():
            self._token = self.oauth_client.get_valid_token()
            # Tool list shouldn't change, but re-fetch to pick up any server-side changes.
            self._tool_schemas = None

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
        """Answer a single question via Claude + Atlassian MCP tools.

        Structurally identical to :meth:`RestAgent.ask` (streaming plus
        ``on_text`` / ``on_tool_call`` / ``on_turn_start`` callbacks), but
        every tool call is dispatched to the MCP server instead of our
        local REST client.
        """
        self._ensure_fresh_token()
        tool_schemas = self._ensure_tool_schemas()

        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"(Confluence space key: {self.space_key})\n\n{question}"
                ),
            }
        ]
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
                tools=tool_schemas,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    streamed_answer.append(text)
                    if on_text is not None:
                        on_text(text)
                final_message = stream.get_final_message()

            input_total += getattr(final_message.usage, "input_tokens", 0) or 0
            output_total += getattr(final_message.usage, "output_tokens", 0) or 0
            messages.append({"role": "assistant", "content": final_message.content})

            if final_message.stop_reason != "tool_use":
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
                    result = asyncio.run(self._call_mcp_tool(name, arguments))
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": _stringify_tool_result(result),
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - surface errors to Claude
                    # Print the real error to stderr so the user sees the
                    # actual cause — otherwise Claude paraphrases it into
                    # a generic apology and the root cause is invisible.
                    # stderr sits outside rich.Live's managed stdout, so
                    # this won't corrupt the live markdown view. anyio's
                    # TaskGroup raises ``ExceptionGroup`` (an ``Exception``
                    # subclass) whose ``str()`` hides the real sub-error;
                    # ``_format_exception`` walks the tree so we see it.
                    detail = _format_exception(exc)
                    print(
                        f"\n\u26a0  MCP tool {name!r} failed:\n{detail}\n",
                        file=sys.stderr,
                        flush=True,
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "is_error": True,
                            "content": f"MCP tool error:\n{detail}",
                        }
                    )

            messages.append({"role": "user", "content": tool_results})

        return AgentResponse(
            answer="(Stopped: exceeded max tool-use iterations before a final answer.)",
            tool_calls=tool_calls_log,
            usage={
                "input": input_total,
                "output": output_total,
                "total": input_total + output_total,
            },
        )


# ---------------------------------------------------------------------------
# MCP → Anthropic tool schema conversion
# ---------------------------------------------------------------------------


def _mcp_tool_to_anthropic(tool: Any) -> dict[str, Any]:
    """Convert an ``mcp.types.Tool`` into the Anthropic tools-API format.

    MCP already uses JSON Schema for input schemas, so this is mostly a
    rename: ``inputSchema`` → ``input_schema``.
    """
    schema = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}
    return {
        "name": tool.name,
        "description": (tool.description or "").strip(),
        "input_schema": schema,
    }
