"""Tests for the REST-mode agent.

We stub out ``anthropic.Anthropic`` entirely so we can drive the
streaming tool-use loop with scripted responses and verify that the
agent:

    * calls tools in the order Claude asks for them,
    * feeds tool results back as ``tool_result`` blocks,
    * streams final text to the caller's ``on_text`` callback,
    * aggregates token usage correctly.
"""

from __future__ import annotations

import copy
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.oauth import TokenBundle
from agent.rest_agent import (
    _DEFAULT_RATE_LIMIT_DELAY,
    _MAX_RETRY_AFTER_SECONDS,
    RestAgent,
    _compute_anthropic_backoff,
    _extract_retry_after,
    _format_tool_call,
    _is_retryable_anthropic_error,
)


# ---------------------------------------------------------------------------
# Stub message / content / stream helpers
# ---------------------------------------------------------------------------


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(block_id: str, name: str, input_: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=input_)


def _message(content, stop_reason: str, input_tokens=10, output_tokens=5) -> SimpleNamespace:
    """Build a final-message stub as returned by ``stream.get_final_message()``."""
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class _FakeStream:
    """Context-manager stub mimicking ``anthropic.Messages.stream``.

    Yields a fixed list of text chunks from ``text_stream`` and returns a
    pre-built final message from ``get_final_message``. Good enough to
    drive both tool-use turns (no text chunks) and final-answer turns
    (text chunks) through the agent's streaming loop.
    """

    def __init__(self, final_message: SimpleNamespace, text_chunks=()) -> None:
        self._final = final_message
        self._chunks = list(text_chunks)

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *exc) -> bool:  # noqa: D401 - context manager protocol
        return False

    @property
    def text_stream(self):
        return iter(self._chunks)

    def get_final_message(self) -> SimpleNamespace:
        return self._final


def _stream_driver(turns):
    """Return a function suitable for ``fake.messages.stream.side_effect``.

    ``turns`` is a list of ``(final_message, text_chunks)`` pairs. Each
    call to ``stream`` yields the next pair and also records a deep copy
    of the messages argument into ``snapshots`` for post-hoc assertions.
    """
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
# _format_tool_call
# ---------------------------------------------------------------------------


def test_format_tool_call_single_query_uses_shortcut() -> None:
    assert _format_tool_call("confluence_search", {"query": "payments outage"}) == (
        'confluence_search("payments outage")'
    )


def test_format_tool_call_multi_arg_uses_kwargs() -> None:
    assert _format_tool_call("get_page", {"id": 589825}) == "get_page(id=589825)"


# ---------------------------------------------------------------------------
# _is_retryable_anthropic_error
# ---------------------------------------------------------------------------


def test_is_retryable_overloaded_by_message() -> None:
    exc = RuntimeError(
        "{'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}}"
    )
    assert _is_retryable_anthropic_error(exc) is True


def test_is_retryable_by_status_code() -> None:
    class FakeStatusError(Exception):
        status_code = 529

    assert _is_retryable_anthropic_error(FakeStatusError("Overloaded")) is True


def test_is_retryable_rejects_auth_errors() -> None:
    class FakeStatusError(Exception):
        status_code = 401

    assert _is_retryable_anthropic_error(FakeStatusError("invalid api key")) is False


def test_is_retryable_rejects_bad_requests() -> None:
    class FakeStatusError(Exception):
        status_code = 400

    assert _is_retryable_anthropic_error(FakeStatusError("context too long")) is False


# ---------------------------------------------------------------------------
# Retry-After extraction and rate-limit backoff
# ---------------------------------------------------------------------------


class _FakeHeaders:
    """Case-insensitive header stub with a dict-like ``get`` method."""

    def __init__(self, mapping: dict) -> None:
        self._mapping = {k.lower(): v for k, v in mapping.items()}

    def get(self, key, default=None):
        return self._mapping.get(key.lower(), default)


class _FakeResponse:
    def __init__(self, headers: dict) -> None:
        self.headers = _FakeHeaders(headers)


class _FakeRateLimit(Exception):
    status_code = 429

    def __init__(self, headers: dict) -> None:
        super().__init__("rate_limit_error")
        self.response = _FakeResponse(headers)


def test_extract_retry_after_parses_numeric_header() -> None:
    exc = _FakeRateLimit({"Retry-After": "7"})
    assert _extract_retry_after(exc) == 7.0


def test_extract_retry_after_handles_lowercase_header() -> None:
    exc = _FakeRateLimit({"retry-after": "3.5"})
    assert _extract_retry_after(exc) == 3.5


def test_extract_retry_after_returns_none_when_missing() -> None:
    exc = _FakeRateLimit({})
    assert _extract_retry_after(exc) is None


def test_extract_retry_after_returns_none_on_non_numeric() -> None:
    exc = _FakeRateLimit({"Retry-After": "Wed, 21 Oct 2025 07:28:00 GMT"})
    assert _extract_retry_after(exc) is None


def test_extract_retry_after_returns_none_when_no_response() -> None:
    exc = _FakeRateLimit({})
    del exc.response  # simulate a differently-shaped SDK error
    assert _extract_retry_after(exc) is None


def test_compute_backoff_uses_retry_after_when_short() -> None:
    exc = _FakeRateLimit({"Retry-After": "4"})
    assert _compute_anthropic_backoff(exc, attempt=0) == 4.0


def test_compute_backoff_returns_none_when_retry_after_exceeds_cap() -> None:
    """Long Retry-After → signal fail-fast with None."""
    exc = _FakeRateLimit({"Retry-After": str(_MAX_RETRY_AFTER_SECONDS + 30)})
    assert _compute_anthropic_backoff(exc, attempt=0) is None


def test_compute_backoff_falls_back_to_default_on_429_without_header() -> None:
    exc = _FakeRateLimit({})
    assert _compute_anthropic_backoff(exc, attempt=0) == _DEFAULT_RATE_LIMIT_DELAY


def test_compute_backoff_uses_exponential_for_non_rate_limit() -> None:
    """Non-429 errors get plain exponential backoff — no retry-after check."""

    class Overloaded(Exception):
        status_code = 529

    exc = Overloaded("Overloaded")
    # attempt=0 → base * 2^0 = 2.0, attempt=1 → 4.0
    assert _compute_anthropic_backoff(exc, attempt=0) == 2.0
    assert _compute_anthropic_backoff(exc, attempt=1) == 4.0


def test_ask_fails_fast_on_tpm_rate_limit_with_long_retry_after() -> None:
    """A 429 with Retry-After > cap should NOT retry — raise on attempt 1."""
    oauth_client = MagicMock()

    class TPMRateLimit(Exception):
        status_code = 429

        def __init__(self) -> None:
            super().__init__(
                "rate_limit_error: 10,000 input tokens per minute"
            )
            self.response = _FakeResponse(
                {"Retry-After": str(_MAX_RETRY_AFTER_SECONDS + 30)}
            )

    fake_anthropic = MagicMock()
    fake_anthropic.messages.stream.side_effect = TPMRateLimit()

    with patch("agent.rest_agent.anthropic.Anthropic", return_value=fake_anthropic), patch(
        "agent.rest_agent.time.sleep"
    ) as sleep_mock:
        agent = RestAgent(
            anthropic_api_key="k",
            oauth_client=oauth_client,
            space_key="PH",
            token_bundle=_valid_token(),
        )
        with pytest.raises(TPMRateLimit):
            agent.ask("hi")

    # Failed on the first attempt, no retries, no sleep.
    assert fake_anthropic.messages.stream.call_count == 1
    sleep_mock.assert_not_called()


def test_ask_retries_short_rate_limit_with_server_delay() -> None:
    """A 429 with Retry-After <= cap should retry after honouring it."""
    oauth_client = MagicMock()

    class ShortRateLimit(Exception):
        status_code = 429

        def __init__(self) -> None:
            super().__init__("rate_limit_error")
            self.response = _FakeResponse({"Retry-After": "3"})

    final = _message([_text_block("ok")], stop_reason="end_turn")
    call_count = {"n": 0}

    def _stream(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ShortRateLimit()
        return _FakeStream(final, ["ok"])

    fake_anthropic = MagicMock()
    fake_anthropic.messages.stream.side_effect = _stream

    with patch("agent.rest_agent.anthropic.Anthropic", return_value=fake_anthropic), patch(
        "agent.rest_agent.time.sleep"
    ) as sleep_mock:
        agent = RestAgent(
            anthropic_api_key="k",
            oauth_client=oauth_client,
            space_key="PH",
            token_bundle=_valid_token(),
        )
        response = agent.ask("hi")

    assert response.answer == "ok"
    assert call_count["n"] == 2
    # The single backoff sleep must have honoured the server's 3-second hint.
    sleep_mock.assert_called_once_with(3.0)


# ---------------------------------------------------------------------------
# RestAgent.ask happy path
# ---------------------------------------------------------------------------


def test_ask_returns_answer_without_tool_use_and_streams_text() -> None:
    oauth_client = MagicMock()

    final = _message(
        [_text_block("42")],
        stop_reason="end_turn",
        input_tokens=100,
        output_tokens=10,
    )
    stream_fn, snapshots = _stream_driver([(final, ["4", "2"])])

    fake_anthropic = MagicMock()
    fake_anthropic.messages.stream.side_effect = stream_fn

    with patch("agent.rest_agent.anthropic.Anthropic", return_value=fake_anthropic):
        agent = RestAgent(
            anthropic_api_key="k",
            oauth_client=oauth_client,
            space_key="PH",
            token_bundle=_valid_token(),
        )
        emitted: list[str] = []
        response = agent.ask("what's the answer?", on_text=emitted.append)

    assert response.answer == "42"
    assert emitted == ["4", "2"]
    assert response.tool_calls == []
    assert response.usage == {"input": 100, "output": 10, "total": 110}
    assert len(snapshots) == 1


def test_ask_executes_tool_use_then_returns_final_answer() -> None:
    oauth_client = MagicMock()

    first = _message(
        [_tool_use_block("tu_1", "confluence_search", {"query": "payments outage"})],
        stop_reason="tool_use",
        input_tokens=200,
        output_tokens=20,
    )
    second = _message(
        [_tool_use_block("tu_2", "get_page", {"id": "589825"})],
        stop_reason="tool_use",
        input_tokens=300,
        output_tokens=30,
    )
    third = _message(
        [_text_block("Here is your answer.")],
        stop_reason="end_turn",
        input_tokens=400,
        output_tokens=40,
    )

    stream_fn, snapshots = _stream_driver(
        [
            (first, []),  # tool_use turn — no text
            (second, []),  # tool_use turn — no text
            (third, ["Here is ", "your answer."]),
        ]
    )

    fake_anthropic = MagicMock()
    fake_anthropic.messages.stream.side_effect = stream_fn

    fake_rest = MagicMock()
    fake_rest.search.return_value = [
        {"id": "589825", "title": "Payments Outage", "excerpt": "...", "url": "u"}
    ]
    fake_rest.get_page.return_value = {
        "id": "589825",
        "title": "Payments Outage",
        "body_text": "full body",
        "url": "u",
    }

    with patch("agent.rest_agent.anthropic.Anthropic", return_value=fake_anthropic):
        agent = RestAgent(
            anthropic_api_key="k",
            oauth_client=oauth_client,
            space_key="PH",
            token_bundle=_valid_token(),
        )
        agent._rest = fake_rest
        emitted_text: list[str] = []
        emitted_tools: list[str] = []
        turn_starts: list[int] = []
        response = agent.ask(
            "why did payments go down?",
            on_text=emitted_text.append,
            on_tool_call=emitted_tools.append,
            on_turn_start=lambda: turn_starts.append(1),
        )

    assert fake_anthropic.messages.stream.call_count == 3
    assert len(snapshots) == 3
    # Second call's history ends with the first tool_result message.
    second_last = snapshots[1][-1]
    assert second_last["role"] == "user"
    assert second_last["content"][0]["type"] == "tool_result"

    assert response.tool_calls == [
        'confluence_search("payments outage")',
        "get_page(id=589825)",
    ]
    assert response.answer == "Here is your answer."
    # Text chunks only go through on_text; tool markers go through on_tool_call.
    assert emitted_text == ["Here is ", "your answer."]
    assert emitted_tools == [
        'confluence_search("payments outage")',
        "get_page(id=589825)",
    ]
    # on_turn_start fires once per Claude roundtrip.
    assert len(turn_starts) == 3
    assert response.usage["input"] == 200 + 300 + 400
    assert response.usage["output"] == 20 + 30 + 40
    assert response.usage["total"] == response.usage["input"] + response.usage["output"]

    fake_rest.search.assert_called_once_with(query="payments outage", limit=10)
    fake_rest.get_page.assert_called_once_with(page_id="589825")


def test_ask_surfaces_tool_errors_to_claude() -> None:
    oauth_client = MagicMock()

    first = _message(
        [_tool_use_block("tu_1", "confluence_search", {"query": "boom"})],
        stop_reason="tool_use",
    )
    second = _message([_text_block("I saw the error.")], stop_reason="end_turn")

    stream_fn, snapshots = _stream_driver(
        [
            (first, []),
            (second, ["I saw the error."]),
        ]
    )

    fake_anthropic = MagicMock()
    fake_anthropic.messages.stream.side_effect = stream_fn

    fake_rest = MagicMock()
    fake_rest.search.side_effect = RuntimeError("http 500")

    with patch("agent.rest_agent.anthropic.Anthropic", return_value=fake_anthropic):
        agent = RestAgent(
            anthropic_api_key="k",
            oauth_client=oauth_client,
            space_key="PH",
            token_bundle=_valid_token(),
        )
        agent._rest = fake_rest
        response = agent.ask("ask")

    tool_result_msg = snapshots[1][-1]
    assert tool_result_msg["role"] == "user"
    assert tool_result_msg["content"][0]["is_error"] is True
    assert "http 500" in tool_result_msg["content"][0]["content"]
    assert response.answer == "I saw the error."


def test_ask_retries_on_anthropic_overloaded_error() -> None:
    """A 529/overloaded on the first stream call should retry transparently."""
    oauth_client = MagicMock()

    class OverloadedError(Exception):
        status_code = 529

    final = _message([_text_block("ok")], stop_reason="end_turn")

    call_count = {"n": 0}

    def _stream(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OverloadedError(
                "{'type': 'error', 'error': {'type': 'overloaded_error', "
                "'message': 'Overloaded'}}"
            )
        return _FakeStream(final, ["ok"])

    fake_anthropic = MagicMock()
    fake_anthropic.messages.stream.side_effect = _stream

    with patch("agent.rest_agent.anthropic.Anthropic", return_value=fake_anthropic), patch(
        "agent.rest_agent.time.sleep"
    ) as sleep_mock:
        agent = RestAgent(
            anthropic_api_key="k",
            oauth_client=oauth_client,
            space_key="PH",
            token_bundle=_valid_token(),
        )
        response = agent.ask("hi")

    assert response.answer == "ok"
    assert call_count["n"] == 2  # initial failure + successful retry
    sleep_mock.assert_called_once()  # exactly one backoff sleep between the two tries


def test_ask_gives_up_after_max_attempts_on_overloaded() -> None:
    """If every retry also fails, the exception is re-raised."""
    oauth_client = MagicMock()

    class OverloadedError(Exception):
        status_code = 529

    def _stream(**kwargs):
        raise OverloadedError("Overloaded")

    fake_anthropic = MagicMock()
    fake_anthropic.messages.stream.side_effect = _stream

    with patch("agent.rest_agent.anthropic.Anthropic", return_value=fake_anthropic), patch(
        "agent.rest_agent.time.sleep"
    ):
        agent = RestAgent(
            anthropic_api_key="k",
            oauth_client=oauth_client,
            space_key="PH",
            token_bundle=_valid_token(),
        )
        with pytest.raises(OverloadedError):
            agent.ask("hi")

    # 3 attempts total (ANTHROPIC_MAX_ATTEMPTS)
    assert fake_anthropic.messages.stream.call_count == 3


def test_ask_does_not_retry_auth_errors() -> None:
    """A 401 should bubble up immediately without retrying."""
    oauth_client = MagicMock()

    class AuthError(Exception):
        status_code = 401

    fake_anthropic = MagicMock()
    fake_anthropic.messages.stream.side_effect = AuthError("bad key")

    with patch("agent.rest_agent.anthropic.Anthropic", return_value=fake_anthropic), patch(
        "agent.rest_agent.time.sleep"
    ) as sleep_mock:
        agent = RestAgent(
            anthropic_api_key="k",
            oauth_client=oauth_client,
            space_key="PH",
            token_bundle=_valid_token(),
        )
        with pytest.raises(AuthError):
            agent.ask("hi")

    assert fake_anthropic.messages.stream.call_count == 1  # no retry
    sleep_mock.assert_not_called()


def test_ask_refreshes_token_when_expired() -> None:
    oauth_client = MagicMock()
    expired = TokenBundle(
        access_token="old",
        refresh_token="r",
        expires_at=time.time() - 100,
        cloud_id="cid",
        site_url="https://your-workspace.atlassian.net",
    )
    fresh = _valid_token()
    oauth_client.get_valid_token.return_value = fresh

    final = _message([_text_block("ok")], stop_reason="end_turn")
    stream_fn, _ = _stream_driver([(final, ["ok"])])

    fake_anthropic = MagicMock()
    fake_anthropic.messages.stream.side_effect = stream_fn

    with patch("agent.rest_agent.anthropic.Anthropic", return_value=fake_anthropic):
        agent = RestAgent(
            anthropic_api_key="k",
            oauth_client=oauth_client,
            space_key="PH",
            token_bundle=expired,
        )
        agent.ask("hi")

    # __init__ was given an explicit bundle, so OAuth is only touched
    # inside ask() via _ensure_fresh_token.
    assert oauth_client.get_valid_token.call_count == 1
    assert agent._token.access_token == fresh.access_token
