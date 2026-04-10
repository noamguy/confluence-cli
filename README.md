# confluence-cli

A Python CLI that answers natural-language questions about a Confluence
workspace using **Claude Sonnet (`claude-sonnet-4-6`)** and one of two
tool backends:

* `--mode rest` — talks to the Confluence REST API directly (recommended).
* `--mode mcp`  — talks to the official Atlassian MCP server at
  `https://mcp.atlassian.com/v1/mcp`.

Both modes authenticate with the **exact same Atlassian OAuth 2.0 token**,
issued via a one-time browser flow and refreshed automatically on
subsequent runs.

---

## Requirements

* Python **3.12.10**
* An Atlassian Cloud site with Confluence (this project is wired to
  `https://your-workspace.atlassian.net`, space key `PH`)
* An Anthropic API key
* An Atlassian OAuth 2.0 (3LO) integration with redirect URI
  `http://localhost:8765/callback`

---

## Setup

```bash
# 1. clone and enter the project
git clone <this repo> confluence-cli
cd confluence-cli

# 2. create and activate a venv (Python 3.12.10)
python3.12 -m venv venv
source venv/bin/activate            # on Windows: venv\Scripts\activate

# 3. install dependencies
pip install -r requirements.txt

# 4. copy the env template and fill in your secrets
cp .env.example .env
$EDITOR .env
```

`.env` should contain:

```
ANTHROPIC_API_KEY=sk-ant-...
ATLASSIAN_CLIENT_ID=your-oauth-client-id
ATLASSIAN_CLIENT_SECRET=your-oauth-client-secret
CONFLUENCE_BASE_URL=https://your-workspace.atlassian.net
CONFLUENCE_SPACE_KEY=PH
```

### Creating the Atlassian OAuth app

1. Go to <https://developer.atlassian.com/console/myapps/> and create a
   new **OAuth 2.0 (3LO)** integration.
2. Add the following Confluence scopes:
   * `read:confluence-content.all`
   * `read:confluence-content.summary`
   * `read:confluence-space.summary`
   * `search:confluence`
   * `offline_access` (required so Atlassian issues a refresh token)
3. Set the callback URL to **`http://localhost:8765/callback`**.
4. Copy the client id and client secret into `.env`.

---

## Running

```bash
# REST mode (recommended)
python main.py --mode rest

# MCP mode
python main.py --mode mcp

# Wipe the cached OAuth token and force a fresh browser flow
python main.py --reset
```

### CLI flags

| Flag | Description |
| ---- | ----------- |
| `--mode {rest,mcp}` | Tool backend. Default: `rest`. |
| `--space SPACE` | Confluence space key. Defaults to `$CONFLUENCE_SPACE_KEY`. |
| `--reset` | Delete the cached OAuth token before running, forcing a fresh browser-based re-auth. Can be combined with `--mode`. |

On the very first run a browser window opens for the OAuth consent
screen. After you approve, the token bundle is persisted to
`~/.confluence-cli/token.json` (permissions `0600`) and refreshed
automatically on subsequent runs. You can override the cache location
with `$CONFLUENCE_CLI_TOKEN_PATH`.

### What a session looks like

The CLI uses [`rich`](https://github.com/Textualize/rich) for live
Markdown rendering, a colored `thinking…` spinner between Claude
roundtrips, and inline tool-call markers that stream above the answer
as each tool fires:

```
╭──────────────────────────────────────────────────────────────────╮
│                                                                  │
│  confluence-cli  —  ask anything about your Confluence space     │
│                                                                  │
│  mode    rest                                                    │
│  model   claude-sonnet-4-6                                       │
│  site    https://your-workspace.atlassian.net                          │
│  space   PH                                                      │
│                                                                  │
│  Try asking:                                                     │
│    • "What do we have in this workspace?"                        │
│    • "What is the last incident we had?"                         │
│                                                                  │
│  Type your question and press Enter.  Type 'exit' or Ctrl+C.     │
│                                                                  │
╰──────────────────────────────────────────────────────────────────╯

? what caused the payments outage last week?

→ confluence_search("payments outage")
→ get_page(id=589825)

The payments outage was caused by a **stale Stripe webhook secret**
after the Oct-3 key rotation. See *Payments Outage Postmortem* for
the full timeline and action items.

[Tools] confluence_search("payments outage") → get_page(id=589825)
[Tokens] input: 1,842 | output: 312 | total: 2,154

? exit
bye.
```

Answers are streamed token-by-token and rendered as Markdown live —
so `**bold**`, `# headers`, bullets, `inline code`, and links all
render properly instead of showing literal asterisks.

Type `exit` (or `quit`, or press Ctrl+C) to leave the REPL.

---

## Running the tests

```bash
source venv/bin/activate
python -m pytest tests/ -q
```

The suite (49 tests, no network) covers:

* `tests/test_oauth.py` — token persistence, expiry/skew, refresh flow,
  cloud-id resolution, and `resolve_token_path` precedence rules.
* `tests/test_tools.py` — tool schemas, storage-format HTML stripping,
  CQL construction, and tool dispatch.
* `tests/test_rest_agent.py` — full REST streaming tool-use loop with
  scripted Claude responses, `on_text` / `on_tool_call` / `on_turn_start`
  callbacks, error surfacing, and token refresh.
* `tests/test_mcp_agent.py` — MCP streaming loop with the hosted MCP
  server fully stubbed out, plus the `_format_exception` unwrapper and
  the transient-error retry classifier.
* `tests/test_main.py` — `--reset` behaviour and argparse wiring.

---

## Project layout

```
confluence-cli/
  main.py               # CLI entry point: argparse, REPL, rich-powered UI
  agent/
    __init__.py
    oauth.py            # Shared Atlassian OAuth 2.0 flow + token cache
    tools.py            # REST tool schemas + ConfluenceRestClient
    rest_agent.py       # Claude + direct Confluence REST API (streaming)
    mcp_agent.py        # Claude + official Atlassian MCP server (streaming,
                        #   with retry on transient Atlassian errors)
  tests/
    __init__.py
    test_oauth.py
    test_tools.py
    test_rest_agent.py
    test_mcp_agent.py
    test_main.py
  requirements.txt
  .env.example
  README.md
```

### Agent architecture

Both agents expose the same streaming callback surface so `main.py` can
drive a consistent rich UI without caring which backend is active:

```python
agent.ask(
    question,
    on_text=...,        # each streamed text chunk from Claude
    on_tool_call=...,   # each tool_use block (formatted call string)
    on_turn_start=...,  # fired before every Claude roundtrip
) -> AgentResponse
```

The REST agent wires tool calls through `agent/tools.py` directly
against `api.atlassian.com/ex/confluence/{cloud_id}/wiki/rest/api`. The
MCP agent opens a streamable-HTTP session to `mcp.atlassian.com/v1/mcp`,
lists tools, and dispatches each `tool_use` back through
`session.call_tool(...)`. The MCP path additionally:

* unwraps `anyio` `ExceptionGroup`s so real sub-exceptions are visible
  in stderr instead of the useless `"unhandled errors in a TaskGroup"`
  summary,
* retries up to 3× with 1s → 2s backoff when the Atlassian MCP server
  returns its `"We are having trouble completing this action. Please
  try again shortly."` transient-error payload.

---

## Approach comparison: Official Atlassian MCP vs. Direct REST

|                               | Official Atlassian MCP                                        | Direct REST API                                        |
| ----------------------------- | ------------------------------------------------------------- | ------------------------------------------------------ |
| Deployment                    | Cloud only                                                    | Cloud + On-Prem                                        |
| Auth                          | OAuth 2.1 (browser JIT) or API token (admin must enable)      | OAuth 2.1 (same token, no admin dependency)            |
| Token usage                   | ~24k tokens per query (full schema injected)                  | Minimal (only your tool definitions)                   |
| Reliability                   | Frequent disconnections, `invalid_token` degradation          | Stable HTTP                                            |
| Confluence toolset            | 11 tools, no attachments/bulk/history                         | Full API surface                                       |
| Jira write ops                | Partial/broken                                                | Full REST API coverage                                 |
| On-prem support               | No                                                            | Yes                                                    |
| SSE deprecation risk          | `/sse` killed June 30 2026 (we use `/v1/mcp`)                 | N/A                                                    |
| Setup complexity              | MCP proxy + OAuth wizard + Node.js dependency                 | Plain HTTP + OAuth token                               |
| Debugging                     | Opaque (MCP protocol layer)                                   | Transparent (standard HTTP logs)                       |

---

## Troubleshooting

**MCP mode answers with "I'm currently experiencing trouble connecting
to Atlassian services":** the Atlassian MCP server backend is degraded.
Watch `stderr` for the real unwrapped error — you'll usually see:

```
⚠  MCP tool 'getAccessibleAtlassianResources' failed:
ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)
  RuntimeError: MCP server returned isError=true for tool '...'.
  Server response: {"error":true,"message":"We are having trouble
  completing this action. Please try again shortly."}
```

The agent auto-retries up to 3 times with 1s → 2s backoff on that
specific message. If all three attempts still fail, the MCP backend is
having a longer outage — **use `--mode rest`**, which hits the same
underlying Confluence REST API directly and bypasses the MCP server
entirely. This is the exact scenario the comparison table above calls
out under "Reliability".

**Expired or wrong-account token:** run `python main.py --reset` to
delete `~/.confluence-cli/token.json` and re-run the browser OAuth flow
from scratch.

**`environment variable ... is not set`:** copy `.env.example` to
`.env` and fill in `ANTHROPIC_API_KEY`, `ATLASSIAN_CLIENT_ID`, and
`ATLASSIAN_CLIENT_SECRET` before running.

---

## Conclusions

The official Atlassian MCP server is a reasonable first step for IDE-integrated,
single-user, Cloud-only workflows. However for production agents it introduces
unnecessary complexity, reliability risks, and token costs.

The direct REST approach delivers the same capabilities — using the identical
OAuth token — with better reliability, lower cost, full API coverage, and no
dependency on Atlassian's hosted MCP infrastructure.

MCP makes sense when: you want zero integration code and you're already in a
supported IDE (Claude Desktop, Cursor).

REST makes sense when: you're building an agent, need reliability, care about
token costs, or serve on-prem customers.
