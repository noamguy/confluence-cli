# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All commands assume the venv is activated:

```bash
source venv/bin/activate            # Windows: venv\Scripts\activate
```

| Task | Command |
| ---- | ------- |
| First-time setup | `python3.12 -m venv venv && source venv/bin/activate && pip install -r requirements.txt` |
| Run CLI (REST mode, default) | `python main.py` |
| Run CLI (MCP mode) | `python main.py --mode mcp` |
| Force a fresh OAuth browser flow | `python main.py --reset` |
| Override the space key at runtime | `python main.py --space OTHER` |
| Run the full test suite | `python -m pytest tests/ -q` |
| Run a single test file | `python -m pytest tests/test_rest_agent.py -q` |
| Run a single test | `python -m pytest tests/test_rest_agent.py::test_ask_executes_tool_use_then_returns_final_answer -q` |
| Syntax-only check across the repo | `python -c "import ast, pathlib; [ast.parse(p.read_text()) for p in pathlib.Path('.').rglob('*.py')]"` |

The test suite makes zero network calls — `requests`, `anthropic.messages.stream`, and the MCP SDK helpers are all stubbed. Tests run in about 2 seconds.

Python is pinned to **3.12.10**. `ExceptionGroup` unwrapping in `agent/mcp_agent.py` relies on 3.11+ semantics, so do not regress the interpreter version.

## Architecture

### Two backends, one shared OAuth, one shared callback surface

The project deliberately implements the same capability twice to compare the official Atlassian MCP server against a direct Confluence REST integration. Understanding the split matters because a change to one mode almost always needs the mirrored change in the other.

```
main.py                         ← rich-powered REPL + argparse + banner
  │
  │ constructs one of ↓ based on --mode
  │
  ├── agent/rest_agent.py       ← Claude + local tools from agent/tools.py
  └── agent/mcp_agent.py        ← Claude + remote tools from mcp.atlassian.com/v1/mcp
         │
         └── both import agent/oauth.py for the shared TokenBundle
```

Both agents expose **exactly** the same public method:

```python
agent.ask(
    question: str,
    on_text: Optional[Callable[[str], None]] = None,       # streamed text chunks
    on_tool_call: Optional[Callable[[str], None]] = None,  # formatted tool call
    on_turn_start: Optional[Callable[[], None]] = None,    # before each Claude roundtrip
) -> AgentResponse
```

`main.py` drives the rich UI purely through these callbacks — the agents are presentation-agnostic. When adding a new agent feature, ensure any new callback or behaviour lands in **both** `rest_agent.py` and `mcp_agent.py` so the two modes stay interchangeable from `main.py`'s perspective.

`AgentResponse` and the shared helpers `_format_tool_call` / `_stringify_tool_result` live in `rest_agent.py` and are imported by `mcp_agent.py` — this is the one intentional coupling between the two files.

### Tool-use loop (both agents)

Both `ask()` implementations run the same streaming Claude loop (`MAX_ITERATIONS = 8` per question, `max_tokens = 2048`):

1. Fire `on_turn_start()`.
2. Open `client.messages.stream(...)` with `tools=<schemas>` and `messages=<history>`.
3. Forward each text chunk to `on_text`.
4. If `stop_reason != "tool_use"`, return; otherwise execute every `tool_use` block, call `on_tool_call(call_str)` for each, append `tool_result` blocks (with `is_error=True` on exceptions) as a single user message, and loop.
5. Tool results are serialized back to Claude via `_stringify_tool_result` (JSON for anything serializable, `str()` fallback).

**Do not** inject status markers via `on_text` — that's what `on_tool_call` is for. Mixing the two was the previous design and caused presentation leaks into the agent.

### REST tool surface (`agent/tools.py`)

Three tools: `confluence_search`, `get_page`, `list_pages_in_space`. The schemas in `TOOL_SCHEMAS` are deliberately minimal — their per-query token cost is a central point of the MCP-vs-REST comparison, so when editing them, favour shorter descriptions over more thorough ones.

`ConfluenceRestClient` targets the OAuth-routed endpoint `https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/rest/api` (the `/wiki/rest/api` v1 surface is still the cleanest for CQL search). Bodies come back as storage-format XHTML and are flattened to plain text by `strip_storage_html` — good enough for Claude, not fidelity-preserving.

`execute_tool(client, name, arguments)` is the dispatch layer between Claude's `tool_use` block and the client methods. Argument shape matches the schemas exactly (e.g. `get_page` gets `id` as a string — see the unquoted-numeric formatting note below).

### MCP-specific concerns (`agent/mcp_agent.py`)

The MCP path has two non-obvious pieces of error handling you must preserve when refactoring:

1. **`_format_exception`** recursively unwraps `BaseExceptionGroup` and chases `__cause__` / `__context__`. The `mcp` SDK runs on `anyio` TaskGroups whose default `str()` is the useless `"unhandled errors in a TaskGroup (1 sub-exception)"`. Always run MCP exceptions through this helper before displaying or logging.

2. **Transient-error retry loop** in `_call_mcp_tool`. The Atlassian MCP server returns a `CallToolResult` with `isError=true` and the literal phrase `"We are having trouble completing this action. Please try again shortly."` during backend degradation. `_is_transient_mcp_error` matches on substrings in `_TRANSIENT_ERROR_MARKERS`; matching errors retry up to `MCP_MAX_ATTEMPTS` times with 1s → 2s backoff. Anything else raises on the first attempt.

Errors from the MCP agent are printed to **stderr** (not via the rich console) so they don't corrupt the rich `Live` region on stdout. The `⚠` glyph is the marker pattern.

A new MCP session is opened per call — we do not reuse the `ClientSession` across `call_tool` invocations. Each call fully opens, initializes, invokes, and tears down. If you change this, verify that `asyncio.run()` still cleans up properly.

### OAuth (`agent/oauth.py`)

Token cache path lookup order: constructor arg → `$CONFLUENCE_CLI_TOKEN_PATH` → `~/.confluence-cli/token.json`. This is exposed as `resolve_token_path()` at module level so `main.py --reset` can find the cache without instantiating `OAuthClient` (which would require env vars the user might not have set yet).

`get_valid_token()` is the only public entry point and is always safe to call — it handles cache hit, expired-with-refresh, and no-cache (interactive browser flow) in one call. If the refresh-token exchange fails with an HTTP error, it silently falls back to a fresh interactive flow rather than bubbling up — this is intentional (never leave the user stuck at a prompt).

The cloud id is resolved via `/oauth/token/accessible-resources` and matched against `$CONFLUENCE_BASE_URL` if set. This lives in `TokenBundle.cloud_id` because the REST v2 URL requires it and we don't want to re-resolve on every call.

### Token expiry and skew

`TokenBundle.is_expired(skew=60)` treats a token as expired **60 seconds before** the real expiry. Both `RestAgent._ensure_fresh_token` and `McpAgent._ensure_fresh_token` call this at the top of every `ask()`. Don't add "optimizations" that skip this check — the skew is specifically there to avoid 401s mid-conversation.

### Tool-call formatting quirk

`_format_tool_call(name, arguments)` in `rest_agent.py` intentionally renders numeric-looking string ids **without quotes** (`get_page(id=589825)`) to match the exact output format specified in the original project requirements. Real Confluence ids come from Claude as JSON strings, so the "isdigit" check in `_render` is load-bearing — removing it breaks `test_format_tool_call_multi_arg_uses_kwargs`.

### Testing patterns worth knowing

- **Messages-list deep-copy trap**: `fake_anthropic.messages.stream.side_effect` receives the `messages` list by reference. The agent continues mutating that list after the call, so `call_args_list[N].kwargs["messages"]` reflects the *final* state, not the state at call time. Both `test_rest_agent.py` and `test_mcp_agent.py` work around this with a `_stream_driver` helper that `copy.deepcopy`s the messages into per-call snapshots. Reuse this pattern for any new test that asserts on historical message state.

- **Fake streaming**: the `_FakeStream` helper mimics `anthropic.Messages.stream`'s context manager. It yields a fixed list of text chunks from `text_stream` and returns a pre-built final-message `SimpleNamespace` from `get_final_message()`. Good enough for both tool-use turns (empty chunks) and final-answer turns (text chunks).

- **MCP async mocking**: patch `McpAgent._call_mcp_tool` with an `async def` replacement (not a MagicMock) — the agent wraps it in `asyncio.run()`, so the replacement must be a real coroutine.

- **OAuth tests never touch the network**: every `requests.post` / `requests.get` is patched. When adding a new OAuth code path, follow the same patching pattern rather than introducing an `httpretty` or `responses` dependency.
