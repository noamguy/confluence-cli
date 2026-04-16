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

from typing import Callable, Optional

import anthropic

from ._claude_loop import AgentResponse, run_tool_use_loop
from .oauth import OAuthClient, TokenBundle
from .tools import TOOL_SCHEMAS, ConfluenceRestClient, execute_tool

#: Hard-coded per the project spec.
MODEL = "claude-sonnet-4-6"

#: System prompt given to Claude. Kept short to keep input tokens low.
SYSTEM_PROMPT = (
    "You are a helpful assistant that answers questions about a Confluence "
    "workspace. You have tools to search and read Confluence pages. Use them "
    "as needed, then answer the user's question concisely and cite the page "
    "titles (and URLs if available) you used."
)


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

        Runs the shared streaming tool-use loop from
        :func:`agent._claude_loop.run_tool_use_loop`.
        """
        self._ensure_fresh_token()
        return run_tool_use_loop(
            anthropic_client=self.anthropic,
            model=self.model,
            system_prompt=SYSTEM_PROMPT,
            tool_schemas=TOOL_SCHEMAS,
            initial_messages=[{"role": "user", "content": question}],
            execute_tool=lambda name, args: execute_tool(self._rest, name, args),
            format_tool_error=lambda exc: f"Tool error: {exc}",
            on_text=on_text,
            on_tool_call=on_tool_call,
            on_turn_start=on_turn_start,
        )

    def get_site_url(self) -> str:
        """Return the Confluence site URL from the current token bundle."""
        return self._token.site_url
