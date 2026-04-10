# confluence-cli

A Python CLI that answers natural-language questions about a Confluence
workspace using **Claude Sonnet (`claude-sonnet-4-6`)** and one of two
tool backends:

* `--mode rest` — talks to the Confluence REST API directly (**recommended**).
* `--mode mcp`  — talks to the official Atlassian MCP server via
  [`mcp-remote`](https://www.npmjs.com/package/mcp-remote).

> ⚠️ **MCP mode requires Node.js v18+.** The hosted Atlassian MCP server
> does not accept standard OAuth bearer tokens directly, so MCP mode
> spawns `mcp-remote` via `npx` as a local stdio proxy. `mcp-remote`
> runs its own separate browser-based auth on first run and caches
> credentials under `~/.mcp-auth/` — completely independent from the
> REST-mode OAuth token. REST mode has no Node.js dependency and is
> the recommended path. See [ANALYSIS.md](./ANALYSIS.md) for the full
> write-up of this limitation (including the rate-limit and
> token-cost implications).

---

## Requirements

| | REST mode | MCP mode |
| --- | --- | --- |
| Python 3.12.10 | ✅ required | ✅ required |
| Anthropic API key | ✅ required | ✅ required |
| **Node.js v18+** on `PATH` | ❌ not needed | ✅ **required** (for `npx` / `mcp-remote`) |
| Atlassian OAuth 2.0 (3LO) app, redirect URI `http://localhost:8765/callback` | ✅ required | ❌ not used (`mcp-remote` handles its own auth) |
| `ATLASSIAN_CLIENT_ID` / `ATLASSIAN_CLIENT_SECRET` env vars | ✅ required | ❌ not needed |
| Interactive browser flow on first run | ✅ (our loopback server on :8765, caches to `~/.confluence-cli/token.json`) | ✅ (separate flow run by `mcp-remote`, caches to `~/.mcp-auth/`) |

Both modes target `https://your-workspace.atlassian.net`, space key
`PH`, by default (override with `--space` or `$CONFLUENCE_SPACE_KEY`).

### Checking Node.js

If you plan to use MCP mode, verify Node.js is installed and on your
`PATH` before you run the CLI:

```bash
node --version   # should print v18.x.x or higher
npx --version    # should print something — npx ships with Node.js
```

If either command fails, install Node.js before continuing:

```bash
# macOS (Homebrew)
brew install node

# or download from https://nodejs.org
```

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

# 4. (MCP mode only) verify Node.js v18+ is installed and on PATH
node --version                        # must print v18.x.x or higher
# if missing:  brew install node       (or https://nodejs.org)

# 5. copy the env template and fill in your secrets
cp .env.example .env
$EDITOR .env
```

`.env` should contain:

```
# required by both modes
ANTHROPIC_API_KEY=sk-ant-...

# REST mode only — MCP mode ignores these
ATLASSIAN_CLIENT_ID=your-oauth-client-id
ATLASSIAN_CLIENT_SECRET=your-oauth-client-secret

CONFLUENCE_BASE_URL=https://your-workspace.atlassian.net
CONFLUENCE_SPACE_KEY=PH
```

### Creating the Atlassian OAuth app (REST mode only)

MCP mode does **not** use this OAuth app — it delegates auth entirely
to `mcp-remote`, which runs its own separate browser flow. The steps
below are only required if you want to use `--mode rest`.

1. Go to <https://developer.atlassian.com/console/myapps/> and create a
   new **OAuth 2.0 (3LO)** integration.
2. Add the following granular scopes:

   **Confluence — View**
   * `read:page:confluence`
   * `read:content-details:confluence`
   * `read:space-details:confluence`
   * `read:comment:confluence`
   * `read:confluence-user`

   **Confluence — Update**
   * `write:page:confluence`
   * `write:comment:confluence`

   **Confluence — Search**
   * `search:confluence`

   **User — View**
   * `read:me`
   * `read:account`

   **Refresh tokens**
   * `offline_access` (not shown on the consent screen, but required so
     Atlassian issues a refresh token)

3. Set the callback URL to **`http://localhost:8765/callback`**.
4. Copy the client id and client secret into `.env`.

> After changing scopes, run `python main.py --reset` to wipe the
> cached token and trigger a fresh browser flow — existing tokens were
> issued under the old scope set and won't pick up the new ones.

---

## Running

```bash
# REST mode (recommended; uses our OAuth token at ~/.confluence-cli/token.json)
python main.py --mode rest

# MCP mode (requires Node.js v18+; mcp-remote opens its own browser
# on first run and caches credentials at ~/.mcp-auth/ — independently
# from the REST token)
python main.py --mode mcp

# Wipe the cached REST-mode OAuth token and force a fresh browser flow.
# Note: this does NOT touch mcp-remote's credentials; to reset those,
# delete ~/.mcp-auth/ manually.
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
| Auth                          | Separate mcp-remote browser flow (`~/.mcp-auth/`); rejects direct bearer tokens | OAuth 2.0 (3LO) we control end-to-end                  |
| Auth lifecycle control        | Opaque — cannot refresh/inspect/reset from our code           | Full control (refresh, --reset, token-path override)   |
| Runtime dependencies          | Python **+ Node.js v18+** (`npx`, `mcp-remote`)               | Python only                                            |
| Token usage                   | ~24k tokens per query (full schema injected)                  | Minimal (only our tool definitions)                    |
| Reliability                   | Frequent disconnections, `invalid_token` degradation          | Stable HTTP                                            |
| Confluence toolset            | 11 tools, no attachments/bulk/history                         | Full API surface                                       |
| Jira write ops                | Partial/broken                                                | Full REST API coverage                                 |
| On-prem support               | No                                                            | Yes                                                    |
| SSE deprecation risk          | `/sse` killed June 30 2026 (we use `/v1/mcp`)                 | N/A                                                    |
| Setup complexity              | OAuth app + Node.js + mcp-remote + *two* auth flows            | Plain HTTP + one OAuth token                           |
| Debugging                     | Opaque (stdio subprocess + MCP protocol layer)                | Transparent (standard HTTP logs)                       |

---

## Troubleshooting

**MCP mode fails with `FileNotFoundError: npx` or `command not found: npx`:**
you don't have Node.js on `PATH`. MCP mode spawns `mcp-remote` via
`npx` as a subprocess. Install Node.js v18+ (e.g. `brew install node`
or from <https://nodejs.org>) and retry. REST mode has no Node.js
dependency.

**MCP mode hangs on first run / no answer appears:** `mcp-remote` is
waiting for you to complete its browser auth flow. It opens a browser
tab at Atlassian's consent screen — approve the requested scopes
there and the CLI will resume. Credentials are cached under
`~/.mcp-auth/` so subsequent runs skip this step.

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
having a longer outage — **use `--mode rest`**, which hits Confluence
directly and bypasses both `mcp-remote` and the hosted MCP server.
This is the exact scenario the comparison table above calls out under
"Reliability".

**Need to reset mcp-remote's own credentials:** delete `~/.mcp-auth/`
(or the relevant subdirectory inside it). Note that `python main.py
--reset` does **not** touch this directory — it only resets REST
mode's OAuth token at `~/.confluence-cli/token.json`. The two auth
stores are completely independent.

**Expired or wrong-account token in REST mode:** run
`python main.py --reset` to delete `~/.confluence-cli/token.json` and
re-run the browser OAuth flow from scratch.

**`environment variable ... is not set`:** copy `.env.example` to
`.env` and fill in `ANTHROPIC_API_KEY`. REST mode additionally needs
`ATLASSIAN_CLIENT_ID` and `ATLASSIAN_CLIENT_SECRET`; MCP mode does not
(mcp-remote handles Atlassian auth out-of-band).

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
