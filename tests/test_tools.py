"""Tests for tool definitions and the Confluence REST client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.tools import (
    TOOL_SCHEMAS,
    ConfluenceRestClient,
    execute_tool,
    strip_storage_html,
)


# ---------------------------------------------------------------------------
# Schema sanity
# ---------------------------------------------------------------------------


def test_tool_schemas_have_required_fields() -> None:
    names = set()
    for schema in TOOL_SCHEMAS:
        assert "name" in schema
        assert "description" in schema
        assert "input_schema" in schema
        assert schema["input_schema"]["type"] == "object"
        names.add(schema["name"])
    assert names == {"confluence_search", "get_page", "list_pages_in_space"}


# ---------------------------------------------------------------------------
# strip_storage_html
# ---------------------------------------------------------------------------


def test_strip_storage_html_removes_tags_and_entities() -> None:
    raw = "<p>Hello &amp; <strong>world</strong></p>"
    assert strip_storage_html(raw) == "Hello & world"


def test_strip_storage_html_collapses_whitespace() -> None:
    raw = "<p>one</p>\n\n<p>two</p>"
    assert strip_storage_html(raw) == "one two"


def test_strip_storage_html_handles_empty() -> None:
    assert strip_storage_html("") == ""


# ---------------------------------------------------------------------------
# ConfluenceRestClient
# ---------------------------------------------------------------------------


def _client() -> ConfluenceRestClient:
    return ConfluenceRestClient(
        access_token="tok",
        cloud_id="cloud-1",
        space_key="PH",
        site_url="https://your-workspace.atlassian.net",
    )


def _fake_get(payload: dict) -> MagicMock:
    fake = MagicMock()
    fake.json.return_value = payload
    fake.raise_for_status = MagicMock()
    return fake


def test_api_base_uses_cloud_id() -> None:
    client = _client()
    assert client.api_base == "https://api.atlassian.com/ex/confluence/cloud-1/wiki/rest/api"


def test_search_builds_cql_and_parses_results() -> None:
    client = _client()
    payload = {
        "results": [
            {
                "excerpt": "<p>Outage <strong>on payments</strong></p>",
                "content": {"id": "123", "title": "Payments Outage"},
            }
        ]
    }
    with patch("agent.tools.requests.get", return_value=_fake_get(payload)) as mget:
        results = client.search("payments outage", limit=5)

    # CQL contains space and query
    called_params = mget.call_args.kwargs["params"]
    assert 'space = "PH"' in called_params["cql"]
    assert "payments outage" in called_params["cql"]
    assert called_params["limit"] == 5

    assert len(results) == 1
    assert results[0]["id"] == "123"
    assert results[0]["title"] == "Payments Outage"
    assert "Outage on payments" in results[0]["excerpt"]
    assert results[0]["url"].endswith("/wiki/spaces/PH/pages/123")


def test_search_clamps_limit_to_max() -> None:
    client = _client()
    payload = {"results": []}
    with patch("agent.tools.requests.get", return_value=_fake_get(payload)) as mget:
        client.search("anything", limit=999)
    assert mget.call_args.kwargs["params"]["limit"] == 25


def test_get_page_returns_plain_text_body() -> None:
    client = _client()
    payload = {
        "id": "456",
        "title": "Runbook",
        "body": {"storage": {"value": "<h1>Steps</h1><p>Do <em>this</em></p>"}},
    }
    with patch("agent.tools.requests.get", return_value=_fake_get(payload)):
        page = client.get_page("456")
    assert page["id"] == "456"
    assert page["title"] == "Runbook"
    assert "Steps" in page["body_text"]
    assert "Do this" in page["body_text"]
    assert page["url"].endswith("/wiki/spaces/PH/pages/456")


def test_list_pages_in_space_returns_titles_and_urls() -> None:
    client = _client()
    payload = {
        "results": [
            {"id": "1", "title": "Page One"},
            {"id": "2", "title": "Page Two"},
        ]
    }
    with patch("agent.tools.requests.get", return_value=_fake_get(payload)) as mget:
        pages = client.list_pages_in_space(limit=10)

    params = mget.call_args.kwargs["params"]
    assert params["spaceKey"] == "PH"
    assert params["limit"] == 10
    assert [p["title"] for p in pages] == ["Page One", "Page Two"]
    assert all("/wiki/spaces/PH/pages/" in p["url"] for p in pages)


# ---------------------------------------------------------------------------
# execute_tool dispatch
# ---------------------------------------------------------------------------


def test_execute_tool_dispatches_search() -> None:
    client = MagicMock()
    client.search.return_value = []
    execute_tool(client, "confluence_search", {"query": "x", "limit": 3})
    client.search.assert_called_once_with(query="x", limit=3)


def test_execute_tool_dispatches_get_page() -> None:
    client = MagicMock()
    client.get_page.return_value = {}
    execute_tool(client, "get_page", {"id": 42})
    client.get_page.assert_called_once_with(page_id="42")


def test_execute_tool_dispatches_list_pages() -> None:
    client = MagicMock()
    client.list_pages_in_space.return_value = []
    execute_tool(client, "list_pages_in_space", {"limit": 5})
    client.list_pages_in_space.assert_called_once_with(limit=5)


def test_execute_tool_rejects_unknown_tool() -> None:
    with pytest.raises(ValueError):
        execute_tool(MagicMock(), "nope", {})
