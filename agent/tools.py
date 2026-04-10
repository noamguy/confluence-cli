"""Direct-REST Confluence tool definitions exposed to Claude.

This module is the "tools" half of the REST-mode agent. It defines:

    * :data:`TOOL_SCHEMAS` — the JSON schemas we hand to Claude so it knows
      what tools it may call and with which arguments.
    * :class:`ConfluenceRestClient` — a thin HTTP wrapper around the
      Confluence Cloud REST API that actually executes those tools.
    * :func:`execute_tool` — dispatches a single tool call coming back from
      Claude to the matching client method.

We deliberately keep the tool surface small and well-documented. The goal
is the opposite of the official Atlassian MCP server's "inject 24k tokens
of schema per query": only the tools we actually need, described in the
fewest tokens that still let Claude use them correctly.
"""

from __future__ import annotations

import html
import re
from typing import Any, Optional

import requests

# ---------------------------------------------------------------------------
# Tool schemas (shown to Claude)
# ---------------------------------------------------------------------------

#: Tool schemas passed to the Anthropic Messages API as ``tools=``.
#:
#: Keep descriptions short but unambiguous — these are the biggest per-query
#: token cost on the REST side, and minimizing them is half the point of this
#: project vs. the official MCP server.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "confluence_search",
        "description": (
            "Full-text search across Confluence pages in the configured space. "
            "Use this first to find candidate pages for any question. "
            "Returns a list of {id, title, excerpt, url}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language or CQL-like search query.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 10, max 25).",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_page",
        "description": (
            "Fetch the full body of a single Confluence page by id. "
            "Returns {id, title, body_text, url}. Body is plain text "
            "extracted from storage-format XHTML."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "The numeric Confluence page id.",
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "list_pages_in_space",
        "description": (
            "List pages in the configured Confluence space. Useful when the user "
            "asks about the overall space contents or a topic they haven't named."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max pages to return (default 25).",
                    "default": 25,
                },
            },
        },
    },
]


# ---------------------------------------------------------------------------
# HTML/storage-format helpers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_storage_html(body: str) -> str:
    """Convert Confluence storage-format XHTML to a rough plain-text string.

    Confluence's "storage" format is an XHTML dialect with ``<ac:…>`` macros.
    We don't need perfect fidelity — Claude just needs readable text to
    reason about — so we strip tags, decode entities, and collapse whitespace.
    """
    if not body:
        return ""
    no_tags = _TAG_RE.sub(" ", body)
    decoded = html.unescape(no_tags)
    return _WS_RE.sub(" ", decoded).strip()


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------


class ConfluenceRestClient:
    """Thin wrapper around the Confluence Cloud REST API.

    We target the ``/wiki/rest/api`` (v1) surface because it still exposes
    CQL search cleanly and is stable for read operations.
    """

    def __init__(
        self,
        access_token: str,
        cloud_id: str,
        space_key: str,
        site_url: Optional[str] = None,
    ) -> None:
        """Create a new REST client.

        Args:
            access_token: OAuth 2.0 bearer token from :class:`OAuthClient`.
            cloud_id:     Atlassian cloud id (numeric) for the site.
            space_key:    Confluence space key to scope searches/listings to.
            site_url:     Base site URL (e.g. ``https://your-workspace.atlassian.net``)
                          used to build human-readable page links.
        """
        self.access_token = access_token
        self.cloud_id = cloud_id
        self.space_key = space_key
        self.site_url = (site_url or "").rstrip("/")
        # OAuth-based Confluence REST calls are routed through api.atlassian.com
        # and addressed by cloud id.
        self.api_base = f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/rest/api"

    # ------------------------------------------------------------------
    # Internal HTTP helper
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """GET an API path and return the decoded JSON body."""
        resp = requests.get(
            f"{self.api_base}{path}",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
            },
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Full-text search using Confluence CQL, scoped to the current space.

        Args:
            query: A free-text query. We wrap it in CQL as ``text ~ "…"``
                   and constrain to the configured space.
            limit: Maximum results to return (clamped to 1..25).
        """
        limit = max(1, min(int(limit or 10), 25))
        cql = f'space = "{self.space_key}" AND type = "page" AND text ~ "{query}"'
        payload = self._get(
            "/search",
            params={"cql": cql, "limit": limit, "expand": "content"},
        )

        results = []
        for item in payload.get("results", []):
            content = item.get("content") or {}
            page_id = content.get("id") or ""
            title = content.get("title") or item.get("title") or ""
            excerpt = strip_storage_html(item.get("excerpt") or "")
            results.append(
                {
                    "id": page_id,
                    "title": title,
                    "excerpt": excerpt,
                    "url": self._page_url(page_id),
                }
            )
        return results

    def get_page(self, page_id: str) -> dict:
        """Fetch a single page by id with its body expanded to storage format."""
        payload = self._get(
            f"/content/{page_id}",
            params={"expand": "body.storage,version,space"},
        )
        body_storage = (
            payload.get("body", {}).get("storage", {}).get("value", "")
        )
        return {
            "id": payload.get("id", page_id),
            "title": payload.get("title", ""),
            "body_text": strip_storage_html(body_storage),
            "url": self._page_url(payload.get("id", page_id)),
        }

    def list_pages_in_space(self, limit: int = 25) -> list[dict]:
        """List pages in the configured space, most recently updated first."""
        limit = max(1, min(int(limit or 25), 100))
        payload = self._get(
            "/content",
            params={
                "spaceKey": self.space_key,
                "type": "page",
                "limit": limit,
                "orderby": "history.lastUpdated desc",
            },
        )
        return [
            {
                "id": p.get("id", ""),
                "title": p.get("title", ""),
                "url": self._page_url(p.get("id", "")),
            }
            for p in payload.get("results", [])
        ]

    # ------------------------------------------------------------------
    # URL helper
    # ------------------------------------------------------------------

    def _page_url(self, page_id: str) -> str:
        """Build a human-readable page URL if we know the site URL."""
        if not page_id or not self.site_url:
            return ""
        return f"{self.site_url}/wiki/spaces/{self.space_key}/pages/{page_id}"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def execute_tool(client: ConfluenceRestClient, name: str, arguments: dict) -> Any:
    """Dispatch a single Claude tool call to the matching REST client method.

    Args:
        client:    A configured :class:`ConfluenceRestClient`.
        name:      Tool name as chosen by Claude (must match a TOOL_SCHEMAS entry).
        arguments: Arguments dict as provided by Claude's ``tool_use`` block.

    Returns:
        A JSON-serializable structure that will be sent back to Claude as
        the ``tool_result`` content.
    """
    if name == "confluence_search":
        return client.search(
            query=arguments["query"],
            limit=arguments.get("limit", 10),
        )
    if name == "get_page":
        return client.get_page(page_id=str(arguments["id"]))
    if name == "list_pages_in_space":
        return client.list_pages_in_space(limit=arguments.get("limit", 25))
    raise ValueError(f"Unknown tool: {name}")
