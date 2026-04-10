"""Shared Atlassian OAuth 2.0 (3LO) flow for confluence-cli.

Both the MCP-backed agent and the direct-REST agent authenticate against
Atlassian using the exact same OAuth token, so the browser dance and
token-cache logic live here in one place.

First-run:  opens a browser, catches the redirect on a local loopback
            server, exchanges the authorization code for an access token,
            persists the token bundle to disk.
Later runs: loads the cached bundle and refreshes it transparently if
            the access token is expired (or close to it).
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import socketserver
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Atlassian OAuth 2.0 authorization endpoint.
AUTH_URL = "https://auth.atlassian.com/authorize"

#: Atlassian OAuth 2.0 token endpoint (used for both code exchange and refresh).
TOKEN_URL = "https://auth.atlassian.com/oauth/token"

#: Endpoint that returns the list of Atlassian sites the user granted access to.
#: We use this to resolve the numeric `cloudid` required by the Confluence REST v2 API.
ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"

#: OAuth scopes requested during the browser flow.
#:
#: This list mirrors the exact consent screen shown by the Atlassian MCP
#: server integration on claude.ai — i.e. the scope set that
#: ``mcp.atlassian.com`` itself expects. Matching it exactly is what
#: lets ``--mode mcp`` work: the MCP server's tools (``atlassianUserInfo``
#: needs ``read:me``; the page/comment tools need the granular
#: ``*:page:confluence`` / ``*:comment:confluence`` scopes) will fail
#: with ``isError=true`` if any of these are missing.
#:
#: ``offline_access`` isn't shown on the consent screen but is required
#: so Atlassian issues a refresh token.
DEFAULT_SCOPES = [
    # --- Confluence: View ---
    "read:page:confluence",
    "read:content-details:confluence",
    "read:space-details:confluence",
    "read:comment:confluence",
    "read:confluence-user",
    # --- Confluence: Update ---
    "write:page:confluence",
    "write:comment:confluence",
    # --- Confluence: Search ---
    "search:confluence",
    # --- User: View ---
    "read:me",
    "read:account",
    # --- Refresh-token issuance ---
    "offline_access",
]

#: Loopback redirect URI the user must also register on their OAuth app.
REDIRECT_URI = "http://localhost:8765/callback"

#: Port for the temporary loopback server. Must match REDIRECT_URI.
LOOPBACK_PORT = 8765

#: Default location for persisted token bundle.
DEFAULT_TOKEN_PATH = Path.home() / ".confluence-cli" / "token.json"

#: Refresh the access token if it expires within this many seconds.
REFRESH_SKEW_SECONDS = 60


def resolve_token_path(override: Optional[Path] = None) -> Path:
    """Return the token cache path using the standard lookup order.

    Order of precedence:
        1. Explicit ``override`` argument
        2. ``$CONFLUENCE_CLI_TOKEN_PATH`` environment variable
        3. :data:`DEFAULT_TOKEN_PATH`

    Exposed at module level so callers like ``main.py --reset`` can find
    the cache file without having to construct a full :class:`OAuthClient`
    (which would require OAuth credentials they may not have loaded yet).
    """
    return Path(
        override
        or os.environ.get("CONFLUENCE_CLI_TOKEN_PATH")
        or DEFAULT_TOKEN_PATH
    ).expanduser()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TokenBundle:
    """An OAuth token bundle, including the Atlassian cloud id.

    Attributes:
        access_token:  Bearer token used for API calls.
        refresh_token: Long-lived token used to obtain new access tokens.
        expires_at:    Absolute POSIX timestamp at which `access_token` expires.
        cloud_id:      The numeric id of the Atlassian site (resolved via
                       ``/oauth/token/accessible-resources``). Needed to build
                       Confluence REST v2 URLs.
        site_url:      The base URL of the Atlassian site (e.g.
                       ``https://your-workspace.atlassian.net``). Kept for display
                       and for constructing links in answers.
    """

    access_token: str
    refresh_token: str
    expires_at: float
    cloud_id: str
    site_url: str

    def is_expired(self, skew: int = REFRESH_SKEW_SECONDS) -> bool:
        """Return True if the access token expires within `skew` seconds."""
        return time.time() >= (self.expires_at - skew)

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict for on-disk persistence."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TokenBundle":
        """Rehydrate a TokenBundle from its serialized dict form."""
        return cls(**data)


# ---------------------------------------------------------------------------
# Loopback HTTP server used to capture the redirect
# ---------------------------------------------------------------------------


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Tiny HTTP handler that captures the ?code=... query parameter.

    We run exactly one of these for the duration of the browser flow, then
    shut the server down. The captured code is written to an attribute on
    the server instance so the main thread can read it.
    """

    def do_GET(self):  # noqa: N802 (name required by BaseHTTPRequestHandler)
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        # Stash whatever we got on the shared server object.
        self.server.auth_code = params.get("code", [None])[0]  # type: ignore[attr-defined]
        self.server.auth_state = params.get("state", [None])[0]  # type: ignore[attr-defined]
        self.server.auth_error = params.get("error", [None])[0]  # type: ignore[attr-defined]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

        if self.server.auth_error:  # type: ignore[attr-defined]
            body = (
                "<html><body><h1>Authorization failed</h1>"
                f"<p>{self.server.auth_error}</p>"  # type: ignore[attr-defined]
                "<p>You can close this window.</p></body></html>"
            )
        else:
            body = (
                "<html><body><h1>confluence-cli authorized</h1>"
                "<p>You can close this window and return to the terminal.</p>"
                "</body></html>"
            )
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format, *args):  # noqa: A002
        """Silence default stderr access logging."""
        return


class _ReusableTCPServer(socketserver.TCPServer):
    """A ``TCPServer`` with ``SO_REUSEADDR`` enabled.

    The OAuth flow opens a short-lived loopback server on
    :data:`LOOPBACK_PORT`. Without ``SO_REUSEADDR``, running the flow
    twice in quick succession (e.g. ``--reset`` immediately after a
    previous run) fails with ``OSError: [Errno 48] Address already in
    use`` because the kernel keeps the previous socket in ``TIME_WAIT``
    for ~60 seconds. Enabling address reuse lets us bind over a stale
    socket from the same user on the same host, which is safe for a
    loopback-only server.
    """

    allow_reuse_address = True


def _run_loopback_server() -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Block until a single OAuth callback request is received.

    Returns:
        A ``(code, state, error)`` tuple. At most one of ``code`` or
        ``error`` will be populated on a correctly behaving auth server.

    Raises:
        RuntimeError: if the loopback port is actually in use by another
            process (as opposed to a stale ``TIME_WAIT`` socket, which
            ``_ReusableTCPServer`` silently handles).
    """
    try:
        with _ReusableTCPServer(
            ("127.0.0.1", LOOPBACK_PORT), _CallbackHandler
        ) as httpd:
            httpd.auth_code = None  # type: ignore[attr-defined]
            httpd.auth_state = None  # type: ignore[attr-defined]
            httpd.auth_error = None  # type: ignore[attr-defined]
            httpd.handle_request()  # exactly one request, then exit
            return (
                httpd.auth_code,  # type: ignore[attr-defined]
                httpd.auth_state,  # type: ignore[attr-defined]
                httpd.auth_error,  # type: ignore[attr-defined]
            )
    except OSError as exc:
        if exc.errno == 48 or "Address already in use" in str(exc):
            raise RuntimeError(
                f"cannot bind OAuth callback server on port {LOOPBACK_PORT}: "
                f"another process is already listening on it. Identify it "
                f"with:\n    lsof -i :{LOOPBACK_PORT}\n"
                f"…then either stop that process or free the port before "
                f"retrying."
            ) from exc
        raise


# ---------------------------------------------------------------------------
# OAuthClient
# ---------------------------------------------------------------------------


class OAuthClient:
    """Manages the Atlassian OAuth flow and persisted token bundle.

    Usage::

        client = OAuthClient(client_id=..., client_secret=...)
        bundle = client.get_valid_token()
        # bundle.access_token is ready to put in an Authorization header
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: Optional[Path] = None,
        scopes: Optional[list[str]] = None,
    ) -> None:
        """Create a new OAuth client.

        Args:
            client_id:     Atlassian OAuth app client id.
            client_secret: Atlassian OAuth app client secret.
            token_path:    Optional override for the token cache file.
            scopes:        Optional override for the OAuth scopes to request.
        """
        if not client_id or not client_secret:
            raise ValueError("client_id and client_secret are required for OAuth")
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes or DEFAULT_SCOPES
        self.token_path = resolve_token_path(token_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_valid_token(self) -> TokenBundle:
        """Return a non-expired TokenBundle, doing the browser flow if needed.

        Order of operations:
            1. If a cached bundle exists and is still valid, return it.
            2. If a cached bundle exists but is expired, refresh it.
            3. Otherwise, run the interactive browser flow.
        """
        bundle = self._load_cached()
        if bundle is None:
            bundle = self._interactive_flow()
        elif bundle.is_expired():
            bundle = self._refresh(bundle)
        self._save(bundle)
        return bundle

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_cached(self) -> Optional[TokenBundle]:
        """Load the cached token bundle from disk, if present and parseable."""
        if not self.token_path.exists():
            return None
        try:
            data = json.loads(self.token_path.read_text())
            return TokenBundle.from_dict(data)
        except (json.JSONDecodeError, TypeError, KeyError):
            # Treat a malformed cache as "no cache".
            return None

    def _save(self, bundle: TokenBundle) -> None:
        """Persist the token bundle with restrictive file permissions."""
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(bundle.to_dict(), indent=2))
        try:
            os.chmod(self.token_path, 0o600)
        except OSError:
            # Best-effort; on some filesystems chmod is not meaningful.
            pass

    # ------------------------------------------------------------------
    # OAuth operations
    # ------------------------------------------------------------------

    def _interactive_flow(self) -> TokenBundle:
        """Run the interactive browser-based authorization code flow."""
        state = secrets.token_urlsafe(24)
        auth_params = {
            "audience": "api.atlassian.com",
            "client_id": self.client_id,
            "scope": " ".join(self.scopes),
            "redirect_uri": REDIRECT_URI,
            "state": state,
            "response_type": "code",
            "prompt": "consent",
        }
        auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(auth_params)}"

        print("Opening browser for Atlassian authorization...")
        print(f"If the browser does not open automatically, visit:\n  {auth_url}\n")

        # Start the loopback server in a background thread so we can fire
        # the browser after it is listening.
        captured: dict = {}

        def _serve():
            # Capture any exception raised by the server so the main
            # thread can re-raise it with proper context. Without this,
            # a socket-bind failure dies silently in the background
            # thread and the main thread falls through to the useless
            # "Timed out waiting for OAuth callback" message.
            try:
                code, got_state, error = _run_loopback_server()
                captured["code"] = code
                captured["state"] = got_state
                captured["error"] = error
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                captured["thread_exc"] = exc

        server_thread = threading.Thread(target=_serve, daemon=True)
        server_thread.start()

        webbrowser.open(auth_url)
        server_thread.join(timeout=300)

        # Surface background-thread failures *before* checking for the
        # callback code, otherwise the user sees "timed out" when the
        # real issue was something like a port collision.
        if "thread_exc" in captured:
            raise captured["thread_exc"]
        if captured.get("error"):
            raise RuntimeError(f"OAuth error: {captured['error']}")
        if not captured.get("code"):
            raise RuntimeError("Timed out waiting for OAuth callback")
        if captured.get("state") != state:
            raise RuntimeError("OAuth state mismatch; possible CSRF attempt")

        return self._exchange_code(captured["code"])

    def _exchange_code(self, code: str) -> TokenBundle:
        """Exchange an authorization code for an access + refresh token."""
        resp = requests.post(
            TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        access_token = payload["access_token"]
        cloud_id, site_url = self._resolve_cloud(access_token)
        return TokenBundle(
            access_token=access_token,
            refresh_token=payload["refresh_token"],
            expires_at=time.time() + int(payload.get("expires_in", 3600)),
            cloud_id=cloud_id,
            site_url=site_url,
        )

    def _refresh(self, bundle: TokenBundle) -> TokenBundle:
        """Use the refresh token to obtain a new access token.

        If the refresh fails (e.g. the refresh token was revoked), we fall
        back to an interactive flow so the user is never left stuck.
        """
        try:
            resp = requests.post(
                TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": bundle.refresh_token,
                },
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.HTTPError:
            print("Refresh token rejected; re-running browser flow.")
            return self._interactive_flow()

        return TokenBundle(
            access_token=payload["access_token"],
            # Atlassian may or may not rotate the refresh token; keep the
            # new one if supplied, otherwise reuse the old one.
            refresh_token=payload.get("refresh_token", bundle.refresh_token),
            expires_at=time.time() + int(payload.get("expires_in", 3600)),
            cloud_id=bundle.cloud_id,
            site_url=bundle.site_url,
        )

    def _resolve_cloud(self, access_token: str) -> tuple[str, str]:
        """Resolve the numeric cloud id and site URL for the Confluence workspace.

        Atlassian's ``accessible-resources`` endpoint returns every site the
        user granted access to. We match on ``CONFLUENCE_BASE_URL`` if set,
        otherwise fall back to the first site returned.
        """
        resp = requests.get(
            ACCESSIBLE_RESOURCES_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        resources = resp.json()
        if not resources:
            raise RuntimeError("No accessible Atlassian resources for this token")

        desired = os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/")
        if desired:
            for r in resources:
                if r.get("url", "").rstrip("/") == desired:
                    return r["id"], r["url"]
        # Fallback: take the first resource.
        return resources[0]["id"], resources[0]["url"]
