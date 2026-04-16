"""Shared test fixtures and helpers for confluence-cli tests.

The stub helpers below (``_text_block``, ``_tool_use_block``, ``_message``,
``_FakeStream``, ``_stream_driver``) are used by both ``test_rest_agent.py``
and ``test_mcp_agent.py`` to drive the streaming tool-use loop with scripted
Anthropic responses. They were previously duplicated between the two files.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace


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
