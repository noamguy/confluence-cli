"""Unit tests for the shared OAuth client.

These tests avoid any real network by patching ``requests`` and skipping
the interactive browser flow. We target the moving parts most likely to
break: persistence, expiry logic, token refresh, and cloud-id resolution.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.oauth import (
    DEFAULT_TOKEN_PATH,
    OAuthClient,
    TokenBundle,
    _run_loopback_server,
    resolve_token_path,
)


# ---------------------------------------------------------------------------
# TokenBundle
# ---------------------------------------------------------------------------


def test_token_bundle_round_trips_through_dict() -> None:
    bundle = TokenBundle(
        access_token="a",
        refresh_token="r",
        expires_at=1234.0,
        cloud_id="cid",
        site_url="https://x.atlassian.net",
    )
    assert TokenBundle.from_dict(bundle.to_dict()) == bundle


def test_token_bundle_is_expired_true_when_past() -> None:
    bundle = TokenBundle(
        access_token="a",
        refresh_token="r",
        expires_at=time.time() - 100,
        cloud_id="cid",
        site_url="https://x.atlassian.net",
    )
    assert bundle.is_expired() is True


def test_token_bundle_is_expired_false_when_future() -> None:
    bundle = TokenBundle(
        access_token="a",
        refresh_token="r",
        expires_at=time.time() + 3600,
        cloud_id="cid",
        site_url="https://x.atlassian.net",
    )
    assert bundle.is_expired() is False


def test_token_bundle_is_expired_respects_skew() -> None:
    bundle = TokenBundle(
        access_token="a",
        refresh_token="r",
        expires_at=time.time() + 30,  # inside default 60s skew
        cloud_id="cid",
        site_url="https://x.atlassian.net",
    )
    assert bundle.is_expired() is True


# ---------------------------------------------------------------------------
# OAuthClient construction
# ---------------------------------------------------------------------------


def test_oauth_client_requires_credentials() -> None:
    with pytest.raises(ValueError):
        OAuthClient(client_id="", client_secret="")


def test_oauth_client_uses_override_token_path(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    client = OAuthClient(client_id="id", client_secret="secret", token_path=path)
    assert client.token_path == path


# ---------------------------------------------------------------------------
# resolve_token_path
# ---------------------------------------------------------------------------


def test_resolve_token_path_prefers_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONFLUENCE_CLI_TOKEN_PATH", str(tmp_path / "env.json"))
    override = tmp_path / "explicit.json"
    assert resolve_token_path(override) == override


def test_resolve_token_path_honours_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / "env.json"
    monkeypatch.setenv("CONFLUENCE_CLI_TOKEN_PATH", str(env_path))
    assert resolve_token_path() == env_path


def test_resolve_token_path_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONFLUENCE_CLI_TOKEN_PATH", raising=False)
    assert resolve_token_path() == DEFAULT_TOKEN_PATH.expanduser()


# ---------------------------------------------------------------------------
# Loopback server port-collision handling
# ---------------------------------------------------------------------------


def test_run_loopback_server_wraps_addr_in_use_as_runtime_error() -> None:
    """Port-collision should become a helpful RuntimeError, not a raw OSError.

    We simulate the bind failure by patching ``_ReusableTCPServer`` to raise
    ``OSError(48, "Address already in use")`` — the exact shape macOS
    produces — and verify that :func:`_run_loopback_server` re-raises it
    as a ``RuntimeError`` mentioning ``lsof`` so the user can diagnose.
    """
    with patch(
        "agent.oauth._ReusableTCPServer",
        side_effect=OSError(48, "Address already in use"),
    ):
        with pytest.raises(RuntimeError, match="lsof -i :8765"):
            _run_loopback_server()


def test_run_loopback_server_re_raises_unrelated_os_errors() -> None:
    """Non-port-collision OSErrors should not be wrapped."""
    with patch(
        "agent.oauth._ReusableTCPServer",
        side_effect=OSError(13, "Permission denied"),
    ):
        with pytest.raises(OSError, match="Permission denied"):
            _run_loopback_server()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _make_client(tmp_path: Path) -> OAuthClient:
    return OAuthClient(
        client_id="id",
        client_secret="secret",
        token_path=tmp_path / "token.json",
    )


def test_load_cached_returns_none_when_missing(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    assert client._load_cached() is None


def test_load_cached_returns_none_on_malformed_json(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    client.token_path.parent.mkdir(parents=True, exist_ok=True)
    client.token_path.write_text("{ not valid json")
    assert client._load_cached() is None


def test_save_and_load_cached_round_trip(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    bundle = TokenBundle(
        access_token="a",
        refresh_token="r",
        expires_at=time.time() + 3600,
        cloud_id="cid",
        site_url="https://x.atlassian.net",
    )
    client._save(bundle)
    loaded = client._load_cached()
    assert loaded == bundle


def test_get_valid_token_returns_cached_when_fresh(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    fresh = TokenBundle(
        access_token="fresh",
        refresh_token="r",
        expires_at=time.time() + 3600,
        cloud_id="cid",
        site_url="https://x.atlassian.net",
    )
    client._save(fresh)
    result = client.get_valid_token()
    assert result.access_token == "fresh"


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def test_refresh_uses_refresh_token_and_keeps_cloud_id(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    old = TokenBundle(
        access_token="old",
        refresh_token="old-refresh",
        expires_at=time.time() - 100,
        cloud_id="cid-123",
        site_url="https://x.atlassian.net",
    )

    fake_response = MagicMock()
    fake_response.json.return_value = {
        "access_token": "new",
        "refresh_token": "new-refresh",
        "expires_in": 3600,
    }
    fake_response.raise_for_status = MagicMock()

    with patch("agent.oauth.requests.post", return_value=fake_response):
        refreshed = client._refresh(old)

    assert refreshed.access_token == "new"
    assert refreshed.refresh_token == "new-refresh"
    assert refreshed.cloud_id == "cid-123"
    assert refreshed.site_url == "https://x.atlassian.net"
    assert refreshed.expires_at > time.time() + 3000


def test_get_valid_token_refreshes_when_expired(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    expired = TokenBundle(
        access_token="old",
        refresh_token="old-refresh",
        expires_at=time.time() - 100,
        cloud_id="cid",
        site_url="https://x.atlassian.net",
    )
    client._save(expired)

    fake_response = MagicMock()
    fake_response.json.return_value = {
        "access_token": "rotated",
        "refresh_token": "rotated-refresh",
        "expires_in": 3600,
    }
    fake_response.raise_for_status = MagicMock()

    with patch("agent.oauth.requests.post", return_value=fake_response):
        result = client.get_valid_token()

    assert result.access_token == "rotated"
    # And it was persisted back to disk.
    on_disk = json.loads(client.token_path.read_text())
    assert on_disk["access_token"] == "rotated"


# ---------------------------------------------------------------------------
# Cloud id resolution
# ---------------------------------------------------------------------------


def test_resolve_cloud_matches_configured_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _make_client(tmp_path)
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://your-workspace.atlassian.net")

    fake_response = MagicMock()
    fake_response.json.return_value = [
        {"id": "other-id", "url": "https://other.atlassian.net"},
        {"id": "target-id", "url": "https://your-workspace.atlassian.net"},
    ]
    fake_response.raise_for_status = MagicMock()

    with patch("agent.oauth.requests.get", return_value=fake_response):
        cloud_id, site_url = client._resolve_cloud("token")

    assert cloud_id == "target-id"
    assert site_url == "https://your-workspace.atlassian.net"


def test_resolve_cloud_falls_back_to_first_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _make_client(tmp_path)
    monkeypatch.delenv("CONFLUENCE_BASE_URL", raising=False)

    fake_response = MagicMock()
    fake_response.json.return_value = [
        {"id": "first", "url": "https://first.atlassian.net"},
        {"id": "second", "url": "https://second.atlassian.net"},
    ]
    fake_response.raise_for_status = MagicMock()

    with patch("agent.oauth.requests.get", return_value=fake_response):
        cloud_id, site_url = client._resolve_cloud("token")

    assert cloud_id == "first"
    assert site_url == "https://first.atlassian.net"
