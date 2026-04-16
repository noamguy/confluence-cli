"""Shared Claude tool-use loop used by both REST and MCP agents.

This module was extracted from :mod:`agent.rest_agent` to eliminate the
coupling where ``mcp_agent.py`` imported retry helpers, formatting
functions, and the ``AgentResponse`` dataclass from ``rest_agent.py``.
None of those are REST-specific — they're generic Claude-tool-loop
infrastructure.

The single public entry point is :func:`run_tool_use_loop`, which
encapsulates the streaming tool-use loop, the Anthropic retry logic,
token-usage accounting, and tool-result sequencing. The four things
that genuinely differ between the two agents are passed in as
parameters: the system prompt, the tool schemas, the ``execute_tool``
callback, and the ``format_tool_error`` callback.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import anthropic

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Max tool-use / model iterations per user question. Prevents runaway loops.
MAX_ITERATIONS = 8

#: Max attempts (including the first try) for a single Anthropic Messages
#: call when the API returns a transient error (overloaded, rate-limited,
#: 5xx, timeout).
ANTHROPIC_MAX_ATTEMPTS = 3

#: Seconds to wait before the first Anthropic retry; subsequent retries
#: use exponential backoff (2s, 4s, …).
ANTHROPIC_RETRY_BASE_DELAY = 2.0

#: HTTP status codes from the Anthropic API that we treat as transient
#: and worth retrying. 529 is Anthropic's "overloaded_error" code.
_RETRYABLE_ANTHROPIC_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})

#: Maximum ``Retry-After`` delay we're willing to honor in an interactive
#: REPL. Longer than this and we fail fast so the user can decide what
#: to do, rather than blocking the CLI for a minute.
_MAX_RETRY_AFTER_SECONDS = 10.0

#: Fallback delay on a 429 response when no ``Retry-After`` header is set.
_DEFAULT_RATE_LIMIT_DELAY = 5.0

#: Substring markers in the error text/body that indicate a transient
#: Anthropic failure, used as a fallback when the exception doesn't
#: carry a structured status code.
_RETRYABLE_ANTHROPIC_MARKERS = (
    "overloaded",
    "rate_limit",
    "rate limit",
    "timeout",
    "temporarily unavailable",
)


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------


def _is_retryable_anthropic_error(exc: BaseException) -> bool:
    """Return True if an Anthropic API exception looks transient.

    We first check for an explicit HTTP ``status_code`` attribute (the
    ``anthropic.APIStatusError`` hierarchy exposes one) against
    :data:`_RETRYABLE_ANTHROPIC_STATUSES`. If that's absent or
    inconclusive, we fall back to substring matching on ``str(exc)`` for
    the words Anthropic actually uses in their error payloads
    (``overloaded``, ``rate_limit``, etc).

    This is intentionally narrow: authentication failures, bad requests,
    and context-length errors are NOT retried — retrying those would
    just burn time on a request that will never succeed.
    """
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and status_code in _RETRYABLE_ANTHROPIC_STATUSES:
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _RETRYABLE_ANTHROPIC_MARKERS)


def _extract_retry_after(exc: BaseException) -> Optional[float]:
    """Return the ``Retry-After`` header value in seconds, if present.

    The anthropic SDK's ``RateLimitError`` exposes the underlying HTTP
    response via ``exc.response``; we walk through it defensively because
    older SDK versions may attach headers differently. Returns ``None``
    if the header is missing, non-numeric, or unreachable.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    # httpx Headers is case-insensitive but we defensively try both.
    raw = None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except (AttributeError, TypeError):
        return None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _compute_anthropic_backoff(
    exc: BaseException, attempt: int
) -> Optional[float]:
    """Return the seconds to sleep before the next Anthropic retry, or None.

    Returns ``None`` to signal "do not retry this one — fail fast". This
    happens when the server-reported ``Retry-After`` exceeds
    :data:`_MAX_RETRY_AFTER_SECONDS`, which is Anthropic's way of telling
    us the token bucket won't refill in an interactive window.

    For rate-limit errors, prefer the server's ``Retry-After`` (capped at
    :data:`_MAX_RETRY_AFTER_SECONDS`); fall back to a small fixed delay
    when the header is absent. For other transient errors (overloaded,
    5xx) use exponential backoff from :data:`ANTHROPIC_RETRY_BASE_DELAY`.
    """
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        retry_after = _extract_retry_after(exc)
        if retry_after is None:
            return _DEFAULT_RATE_LIMIT_DELAY
        if retry_after > _MAX_RETRY_AFTER_SECONDS:
            # Server says "wait longer than we're willing to block the
            # REPL for". Fail fast so main.py can render a context-aware
            # error and let the user decide.
            return None
        return retry_after
    # Non-rate-limit transient errors use plain exponential backoff.
    return ANTHROPIC_RETRY_BASE_DELAY * (2 ** attempt)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class AgentResponse:
    """Structured result returned by both agent ``ask()`` methods.

    Attributes:
        answer:     The final assistant text answer.
        tool_calls: A list of ``"tool_name(arg=value, ...)"`` strings for
                    display in the ``[Tools]`` footer.
        usage:      A dict with ``input``, ``output``, and ``total`` token counts.
    """

    answer: str
    tool_calls: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0, "total": 0})


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


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


def _stringify_tool_result(result: Any) -> str:
    """Serialize a tool result into a compact JSON string for Claude."""
    try:
        return json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(result)


# ---------------------------------------------------------------------------
# The shared tool-use loop
# ---------------------------------------------------------------------------


def run_tool_use_loop(
    anthropic_client: anthropic.Anthropic,
    model: str,
    system_prompt: str,
    tool_schemas: list[dict],
    initial_messages: list[dict],
    execute_tool: Callable[[str, dict], Any],
    format_tool_error: Callable[[Exception], str],
    on_text: Optional[Callable[[str], None]] = None,
    on_tool_call: Optional[Callable[[str], None]] = None,
    on_turn_start: Optional[Callable[[], None]] = None,
    max_iterations: int = MAX_ITERATIONS,
) -> AgentResponse:
    """Run the streaming Claude tool-use loop.

    This encapsulates the ``for _ in range(MAX_ITERATIONS)`` loop, the
    Anthropic retry loop with backoff, the streaming text-chunk forwarding,
    token-usage accounting, tool-use-block extraction, and tool-result
    sequencing.

    Args:
        anthropic_client: Configured Anthropic SDK client.
        model:            Claude model id.
        system_prompt:    System prompt for Claude.
        tool_schemas:     Tool definitions in Anthropic format.
        initial_messages: Starting message list (typically one user message).
        execute_tool:     ``(name, args) -> result`` callback.
        format_tool_error: ``(exc) -> str`` callback for tool error content.
        on_text:          Optional callback for streamed text chunks.
        on_tool_call:     Optional callback for formatted tool-call strings.
        on_turn_start:    Optional callback fired before each Claude roundtrip.
        max_iterations:   Max tool-use iterations (default :data:`MAX_ITERATIONS`).
    """
    messages: list[dict[str, Any]] = list(initial_messages)
    tool_calls_log: list[str] = []
    input_total = 0
    output_total = 0
    streamed_answer: list[str] = []

    for _ in range(max_iterations):
        if on_turn_start is not None:
            on_turn_start()

        # Retry loop for transient Anthropic API errors (overloaded,
        # rate-limited, 5xx). We only retry if NO text has streamed
        # yet — retrying mid-stream would show the user a garbled
        # answer followed by a second, different answer.
        final_message = None
        for attempt in range(ANTHROPIC_MAX_ATTEMPTS):
            attempt_streamed_any = False
            try:
                with anthropic_client.messages.stream(
                    model=model,
                    max_tokens=2048,
                    system=system_prompt,
                    tools=tool_schemas,
                    messages=messages,
                ) as stream:
                    for text in stream.text_stream:
                        attempt_streamed_any = True
                        streamed_answer.append(text)
                        if on_text is not None:
                            on_text(text)
                    final_message = stream.get_final_message()
                break  # success — exit retry loop
            except Exception as exc:  # noqa: BLE001
                is_last = attempt == ANTHROPIC_MAX_ATTEMPTS - 1
                if (
                    attempt_streamed_any
                    or is_last
                    or not _is_retryable_anthropic_error(exc)
                ):
                    raise
                delay = _compute_anthropic_backoff(exc, attempt)
                if delay is None:
                    # Server-reported Retry-After exceeds our cap —
                    # typically a TPM rate-limit that won't refill in
                    # an interactive window. Fail fast so main.py can
                    # render a friendly, mode-aware error.
                    raise
                print(
                    f"  \u21bb Anthropic API returned a transient error "
                    f"({type(exc).__name__}); retrying in {delay:.0f}s "
                    f"(attempt {attempt + 2}/{ANTHROPIC_MAX_ATTEMPTS})…",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)

        # Defensive: every code path above either assigned
        # final_message or re-raised.
        assert final_message is not None

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
                result = execute_tool(name, arguments)
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
                        "content": format_tool_error(exc),
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
