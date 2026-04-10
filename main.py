"""confluence-cli entry point.

A small REPL that answers natural-language questions about a Confluence
workspace using Claude Sonnet (claude-sonnet-4-6) and one of two tool
backends:

    * ``--mode rest`` — direct Confluence REST API (recommended)
    * ``--mode mcp``  — official Atlassian MCP server

Both modes share the same OAuth 2.0 flow, defined in :mod:`agent.oauth`.

All presentation (banner, live Markdown streaming, colored spinner,
tool-call markers) lives here so the agents stay presentation-agnostic.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Callable, Optional, Protocol

from dotenv import load_dotenv
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from agent.mcp_agent import McpAgent
from agent.oauth import OAuthClient, resolve_token_path
from agent.rest_agent import AgentResponse, RestAgent

#: Shared rich console — used for the banner, REPL prompt, live region,
#: tool markers, and footer lines.
console = Console()


# ---------------------------------------------------------------------------
# Small protocol so the REPL can treat both agents uniformly.
# ---------------------------------------------------------------------------


class _Agent(Protocol):
    """Structural type shared by :class:`RestAgent` and :class:`McpAgent`."""

    def ask(
        self,
        question: str,
        on_text: Optional[Callable[[str], None]] = None,
        on_tool_call: Optional[Callable[[str], None]] = None,
        on_turn_start: Optional[Callable[[], None]] = None,
    ) -> AgentResponse: ...


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for the top-level CLI."""
    parser = argparse.ArgumentParser(
        prog="confluence-cli",
        description=(
            "Ask natural-language questions about a Confluence workspace, "
            "powered by Claude Sonnet (claude-sonnet-4-6)."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("mcp", "rest"),
        default="rest",
        help="Tool backend: 'rest' (direct Confluence REST API) or 'mcp' "
        "(official Atlassian MCP server). Default: rest.",
    )
    parser.add_argument(
        "--space",
        default=None,
        help="Override Confluence space key (defaults to $CONFLUENCE_SPACE_KEY).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete the cached OAuth token before running. Forces a fresh "
        "browser-based re-auth on the next call.",
    )
    return parser


def print_banner(mode: str, space_key: str, site_url: str) -> None:
    """Print a styled welcome banner inside a rich Panel."""
    # MCP mode runs a separate auth session via mcp-remote (Node.js),
    # so its "site" line is meaningless to us and the user needs a
    # heads-up that a browser will open on first run.
    if mode == "mcp":
        site_line = "[yellow](managed by mcp-remote)[/yellow]"
        extra_auth_note = (
            "\n[yellow]⚠ MCP mode uses Atlassian's own auth — a browser "
            "window will open on first run.[/yellow]\n"
            "[dim]  (mcp-remote caches credentials under ~/.mcp-auth/ "
            "independently from our OAuth token.)[/dim]\n"
        )
        node_note = (
            "[dim]  Requires Node.js v18+ on PATH for the 'npx' / "
            "'mcp-remote' subprocess.[/dim]\n"
        )
    else:
        site_line = site_url or "(resolving…)"
        extra_auth_note = ""
        node_note = ""

    body = Text.from_markup(
        "[bold cyan]confluence-cli[/bold cyan]  —  "
        "ask anything about your Confluence space\n\n"
        f"[dim]mode   [/dim] [bold]{mode}[/bold]\n"
        "[dim]model  [/dim] claude-sonnet-4-6\n"
        f"[dim]site   [/dim] {site_line}\n"
        f"[dim]space  [/dim] {space_key}\n"
        f"{extra_auth_note}{node_note}\n"
        "[dim]Try asking:[/dim]\n"
        '  [cyan]•[/cyan] [italic]"What do we have in this workspace?"[/italic]\n'
        '  [cyan]•[/cyan] [italic]"What is the last incident we had?"[/italic]\n\n'
        "[dim]Type your question and press Enter.  "
        "Type 'exit' or press Ctrl+C to quit.[/dim]"
    )
    console.print(Panel(body, border_style="cyan", padding=(1, 2)))
    console.print()


def format_tool_footer(tool_calls: list[str]) -> Text:
    """Render the ``[Tools]`` footer line as a styled Text."""
    if not tool_calls:
        return Text.from_markup("[dim cyan][Tools][/dim cyan] [dim](none)[/dim]")
    joined = " → ".join(tool_calls)
    return Text.from_markup(
        f"[dim cyan][Tools][/dim cyan] [dim]{joined}[/dim]"
    )


def format_token_footer(usage: dict) -> Text:
    """Render the ``[Tokens]`` footer line as a styled Text."""
    return Text.from_markup(
        "[dim cyan][Tokens][/dim cyan] "
        f"[dim]input: {usage.get('input', 0):,} | "
        f"output: {usage.get('output', 0):,} | "
        f"total: {usage.get('total', 0):,}[/dim]"
    )


# ---------------------------------------------------------------------------
# Agent construction
# ---------------------------------------------------------------------------


def reset_cached_token() -> None:
    """Delete the persisted OAuth token bundle, if one exists.

    Uses the same path-resolution rules as :class:`OAuthClient` so the
    ``--reset`` flag honours ``$CONFLUENCE_CLI_TOKEN_PATH``.
    """
    path = resolve_token_path()
    if path.exists():
        path.unlink()
        console.print(
            f"[green]\u2713[/green] removed cached token at [dim]{path}[/dim]"
        )
    else:
        console.print(
            f"[dim]no cached token at {path} — nothing to remove[/dim]"
        )


def _require_env(name: str) -> str:
    """Return an environment variable or exit with a clear error."""
    value = os.environ.get(name)
    if not value:
        console.print(
            f"[red]error:[/red] environment variable [bold]{name}[/bold] is not set"
        )
        console.print("       copy .env.example to .env and fill it in")
        sys.exit(2)
    return value


def build_agent(mode: str, space_key: str) -> _Agent:
    """Construct the chosen agent, running OAuth if needed.

    REST mode needs our own Atlassian OAuth client id/secret — the agent
    will open a browser on first run and cache a token under
    ``~/.confluence-cli/token.json``. MCP mode does **not** use those
    env vars at all because mcp-remote runs its own separate auth flow
    as an ``npx`` subprocess.
    """
    anthropic_key = _require_env("ANTHROPIC_API_KEY")

    if mode == "rest":
        client_id = _require_env("ATLASSIAN_CLIENT_ID")
        client_secret = _require_env("ATLASSIAN_CLIENT_SECRET")
        oauth_client = OAuthClient(
            client_id=client_id, client_secret=client_secret
        )
        return RestAgent(
            anthropic_api_key=anthropic_key,
            oauth_client=oauth_client,
            space_key=space_key,
        )

    # MCP mode: mcp-remote (Node.js) handles Atlassian auth independently.
    return McpAgent(
        anthropic_api_key=anthropic_key,
        space_key=space_key,
    )


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------


def _run_question(agent: _Agent, question: str) -> None:
    """Run a single question through the agent with live UI.

    Sets up a rich Live display that shows:
        * a colored ``thinking…`` spinner between Claude roundtrips,
        * a progressively rendered Markdown view of the streamed answer,
        * colored ``→ tool_name(...)`` markers printed above the live
          region as each tool is invoked.
    """
    # Shared state between the three callbacks and the Live renderable.
    state = {"mode": "thinking", "text": ""}
    thinking_spinner = Spinner(
        "dots",
        text=Text("thinking…", style="cyan"),
        style="cyan",
    )

    def _renderable():
        """Build the current Live renderable from `state`.

        When thinking we show (any already-streamed text) + spinner; when
        streaming we show the markdown alone. Empty state renders as a
        single blank line so Live has something to draw.
        """
        parts = []
        if state["text"]:
            parts.append(Markdown(state["text"]))
        if state["mode"] == "thinking":
            parts.append(thinking_spinner)
        return Group(*parts) if parts else Text("")

    with Live(
        _renderable(),
        console=console,
        refresh_per_second=12,
        transient=False,
    ) as live:

        def on_turn_start() -> None:
            state["mode"] = "thinking"
            live.update(_renderable())

        def on_text(chunk: str) -> None:
            state["mode"] = "streaming"
            state["text"] += chunk
            live.update(_renderable())

        def on_tool_call(call_str: str) -> None:
            # Print above the live region so each tool invocation scrolls
            # into the history as it happens, while the spinner stays put.
            console.print(
                Text.from_markup(f"[dim cyan]→[/dim cyan] [cyan]{call_str}[/cyan]")
            )

        response = agent.ask(
            question,
            on_text=on_text,
            on_tool_call=on_tool_call,
            on_turn_start=on_turn_start,
        )

        # Last-ditch fallback: if the final answer never streamed (e.g. the
        # iteration budget was exhausted), surface it inside the live region
        # before we exit the context so the user sees *something*.
        if not state["text"] and response.answer:
            state["text"] = response.answer
            state["mode"] = "streaming"
            live.update(_renderable())

    console.print()
    console.print(format_tool_footer(response.tool_calls))
    console.print(format_token_footer(response.usage))
    console.print()


def _render_error(exc: BaseException, mode: str) -> None:
    """Print a context-aware error panel for a failed question.

    Specialised rendering for the common transient-API failures we
    already tried to retry before giving up, with mode-specific
    guidance for the ones where mode matters (e.g. rate limits in MCP
    mode are caused by the schema injection the comparison table warns
    about — pointing the user at ``--mode rest`` is the actual fix).
    """
    exc_name = type(exc).__name__
    exc_text = str(exc)
    lowered = exc_text.lower()

    # Rate limit (429) — especially painful in MCP mode.
    status_code = getattr(exc, "status_code", None)
    is_rate_limit = status_code == 429 or "rate_limit" in lowered or "rate limit" in lowered

    if is_rate_limit:
        console.print(
            "\n[red]error:[/red] [bold]Anthropic rate limit hit[/bold]"
        )
        if mode == "mcp":
            console.print(
                "  [yellow]This is the failure mode the README comparison "
                "table warns about under [bold]Token usage[/bold].[/yellow]\n"
                "  The hosted Atlassian MCP server injects its full tool "
                "schema (~24k input tokens) on [italic]every[/italic] "
                "Claude call, which blows straight past the default\n"
                "  10,000 input-tokens-per-minute budget on a single "
                "question — no amount of retry will fix it within an "
                "interactive session."
            )
            console.print(
                "\n  [bold green]Fix:[/bold green] switch to REST mode, "
                "which sends ~2k tokens of tool schemas instead of ~24k:"
            )
            console.print(
                "    [cyan]python main.py --mode rest[/cyan]"
            )
            console.print(
                "\n  [dim]Or wait ~60s for your token bucket to refill "
                "and retry in MCP mode.[/dim]"
            )
        else:
            console.print(
                "  Your org has a 10,000 input-tokens-per-minute budget "
                "on claude-sonnet-4-6 and you've burned through it.\n"
                "  Wait ~60s for the bucket to refill and retry. If this "
                "keeps happening on REST mode, contact Anthropic sales\n"
                "  about a rate-limit increase."
            )
        return

    if "overloaded" in lowered:
        console.print(
            "\n[red]error:[/red] Anthropic API is overloaded — we "
            "retried 3 times with backoff and they're still shedding "
            "load. Please try your question again in a moment."
        )
        return

    console.print(f"\n[red]error:[/red] [bold]{exc_name}[/bold] {exc_text}")


def repl(agent: _Agent, mode: str) -> int:
    """Read-eval-print loop. Returns a process exit code."""
    while True:
        try:
            question = console.input("[bold cyan]?[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            console.print("[dim]bye.[/dim]")
            return 0

        if not question:
            continue
        if question.lower() in {"exit", "quit"}:
            console.print("[dim]bye.[/dim]")
            return 0

        console.print()

        try:
            _run_question(agent, question)
        except KeyboardInterrupt:
            console.print()
            console.print("[yellow](interrupted)[/yellow]")
            continue
        except Exception as exc:  # noqa: BLE001
            _render_error(exc, mode)
            continue


def main(argv: list[str] | None = None) -> int:
    """Program entry point."""
    load_dotenv()
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # Reset happens before agent construction so the next OAuth call
    # (triggered by build_agent → OAuthClient.get_valid_token) runs a
    # fresh interactive browser flow.
    if args.reset:
        reset_cached_token()

    space_key = args.space or os.environ.get("CONFLUENCE_SPACE_KEY", "PH")
    agent = build_agent(args.mode, space_key)

    # Best-effort: show site URL in the banner if the agent has a token bundle.
    site_url = getattr(getattr(agent, "_token", None), "site_url", "") or ""
    print_banner(args.mode, space_key, site_url)

    return repl(agent, args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
