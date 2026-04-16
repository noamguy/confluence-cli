# Refactor Plan

This plan was produced after auditing the repo with five parallel explore
agents focused on code duplication, module boundaries, test quality,
error-handling coherence, and dead code / complexity hotspots. Everything
below is a *pure refactor* — no behaviour changes, just consolidation and
boundary cleanup. The existing 77-test suite is strong enough to catch
regressions.

**Status:** completed. All steps executed successfully — 77 tests pass,
both agents delegate to `run_tool_use_loop()`, no cross-imports remain.

---

## The problem in one paragraph

`agent/mcp_agent.py` imports `AgentResponse`, `_format_tool_call`,
`_stringify_tool_result`, `_is_retryable_anthropic_error`,
`_compute_anthropic_backoff`, and `ANTHROPIC_MAX_ATTEMPTS` from
`agent/rest_agent.py`. None of those are REST-specific — they're generic
Claude-tool-loop infrastructure that ended up in `rest_agent.py` by
accident of implementation order. Separately, the two agents' `ask()`
methods are **~85% identical by line count**: both run the same
streaming tool-use loop, the same Anthropic retry loop, the same
`attempt_streamed_any` fail-fast guard, the same token-usage accounting,
and the same tool-use-block extraction. Only four things genuinely
differ: the system prompt, the tool-schema source, the `execute_tool`
call, and the tool-error formatting. Plus test helpers are
copy-pasted between `test_rest_agent.py` and `test_mcp_agent.py`.

---

## The six changes, in order

### 1. New file: `agent/_claude_loop.py`

Move the following from `agent/rest_agent.py` verbatim (no behaviour
change):

- `AgentResponse` (dataclass)
- `_format_tool_call`, `_stringify_tool_result`
- Constants: `ANTHROPIC_MAX_ATTEMPTS`, `ANTHROPIC_RETRY_BASE_DELAY`,
  `_RETRYABLE_ANTHROPIC_STATUSES`, `_RETRYABLE_ANTHROPIC_MARKERS`,
  `_MAX_RETRY_AFTER_SECONDS`, `_DEFAULT_RATE_LIMIT_DELAY`
- Helpers: `_is_retryable_anthropic_error`, `_extract_retry_after`,
  `_compute_anthropic_backoff`
- `MAX_ITERATIONS = 8` (currently duplicated between `rest_agent.py`
  and `mcp_agent.py`)

Then add the new extracted function:

```python
def run_tool_use_loop(
    anthropic_client: anthropic.Anthropic,
    model: str,
    system_prompt: str,
    tool_schemas: list[dict],
    initial_messages: list[dict],
    execute_tool: Callable[[str, dict], Any],         # name, args -> result
    format_tool_error: Callable[[Exception], str],     # exc -> tool_result content
    on_text: Optional[Callable[[str], None]] = None,
    on_tool_call: Optional[Callable[[str], None]] = None,
    on_turn_start: Optional[Callable[[], None]] = None,
    max_iterations: int = MAX_ITERATIONS,
) -> AgentResponse
```

This encapsulates: the `for _ in range(MAX_ITERATIONS)` loop, the
Anthropic retry loop with `_compute_anthropic_backoff`, the streaming
text-chunk forwarding, the `attempt_streamed_any` fail-fast guard,
token-usage accounting, the tool-use-block extraction, and the
tool-result sequencing. The four things that genuinely differ become
parameters.

**Preserve exactly:**
- `time.sleep` (not `asyncio.sleep`) for the Anthropic retry — the loop
  runs from a sync context in both agents.
- The "do not retry if text has already streamed" invariant.
- The `raise` on `None` return from `_compute_anthropic_backoff` (the
  TPM-exhaustion fail-fast path).
- Stderr `↻` retry-marker printing (for now — see §6 below for a
  longer-term improvement).

### 2. Migrate `agent/rest_agent.py`

- Replace the duplicated constants/helpers with imports from
  `agent._claude_loop`.
- `RestAgent.ask()` shrinks from ~158 lines to ~20:

  ```python
  def ask(self, question, on_text=None, on_tool_call=None, on_turn_start=None):
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
  ```

- `REST_SYSTEM_PROMPT` stays in `rest_agent.py` (it's REST-specific phrasing).
- Run `pytest tests/test_rest_agent.py tests/test_tools.py -q` after
  this step — should be green before proceeding.

### 3. Migrate `agent/mcp_agent.py`

- Drop the `from .rest_agent import ...` block; import from
  `agent._claude_loop` instead.
- `McpAgent.ask()` shrinks similarly. The `execute_tool` callback is
  `lambda name, args: asyncio.run(self._call_mcp_tool(name, args))`,
  and `format_tool_error` uses `_format_exception(exc)` (keep the
  stderr diagnostic print — it's still load-bearing for the hidden-
  error pathology documented in ANALYSIS.md).
- `MCP_SYSTEM_PROMPT`, the initial-message format with the space key
  prefix, and all MCP-transport plumbing (`_stdio_params`,
  `_list_mcp_tools`, `_call_mcp_tool`, `_ensure_tool_schemas`,
  `_is_transient_mcp_error`, `_format_exception`, the MCP retry
  constants) stay in `mcp_agent.py` — they're genuinely MCP-specific.
- Run the full suite after this step.

### 4. New file: `tests/conftest.py`

Move these shared test helpers from `test_rest_agent.py` and
`test_mcp_agent.py`:

- `_text_block(text)`
- `_tool_use_block(block_id, name, input_)`
- `_message(content, stop_reason, input_tokens, output_tokens)`
- `_FakeStream` class (the `anthropic.Messages.stream` stub)
- `_stream_driver(turns)` helper (returns `(stream_fn, snapshots)`;
  uses `copy.deepcopy` per call to work around mock-ref mutation)

Both files currently have byte-identical copies. After the move, both
test files import from `conftest` via pytest's auto-discovery and the
duplication disappears.

Keep the tests for `_valid_token()` in `test_rest_agent.py` (it's
REST-specific — MCP mode no longer uses `TokenBundle`).

### 5. Small boundary fixes (bundle these in the same PR)

- **`agent/oauth.py:1-11` docstring.** Currently claims "Both the
  MCP-backed agent and the direct-REST agent authenticate against
  Atlassian using the exact same OAuth token". This has been false
  since the mcp-remote refactor. Rewrite to say the module is
  REST-mode only, with a pointer to mcp-remote's independent auth for
  MCP mode.
- **`main.py` private-attribute access.** Line ~406 does
  `getattr(agent, "_token", None)` to extract the site URL for the
  banner. Add a public `get_site_url() -> str` method on both
  `RestAgent` (returns `self._token.site_url`) and `McpAgent`
  (returns `""` or `"(managed by mcp-remote)"`). Update `main.py` to
  call that instead.
- **`.env.example` comments.** Add an inline comment block above
  `ATLASSIAN_CLIENT_ID` saying the OAuth 3LO creds are REST-mode only
  and MCP mode ignores them (mcp-remote handles its own auth).

### 6. Things deliberately NOT in scope

Document these as "considered and rejected" in the PR description so
a future audit doesn't re-surface them:

- **Printing REST tool errors to stderr for symmetry with MCP.** The
  reason MCP needs stderr output is its specific pathology of errors-
  as-data (`isError=true` in `CallToolResult.content`) which Claude
  paraphrases away. REST errors come through as `requests.HTTPError`
  exceptions that Claude handles cleanly. No duplicate rendering.
- **Catching `except Exception` instead of `except RuntimeError` in
  `mcp_agent.py:250`.** Current behaviour is "ExceptionGroup
  transport errors bypass the transient retry and go straight to
  Claude as `is_error=true`". That's defensible — transport errors
  aren't the same class of thing as server-reported transient
  errors. Add a short comment explaining the deliberate scope
  instead of changing the behaviour.
- **Making retry markers callback-driven instead of stderr prints.**
  This would push presentation into `main.py` (cleaner layering), but
  it materially complicates the retry-inside-streaming flow and the
  current stderr prints are already outside `rich.Live`'s managed
  stdout so they don't corrupt the UI. Revisit if we ever add a
  non-rich presentation layer.
- **Coverage additions for `OAuthClient._interactive_flow()` and
  `main._render_error()`.** Worth doing eventually but not in this
  refactor — they'd require mocking `webbrowser.open`, threading
  plumbing, and rich console capture, for moderate payoff.

---

## Execution order (ordered checklist)

1. [ ] Create `agent/_claude_loop.py` with moved constants + helpers
       (no `run_tool_use_loop` function yet). Update `rest_agent.py`
       imports to point at the new module. Run tests; should be green.
2. [ ] Add `run_tool_use_loop` to `_claude_loop.py`. Write a tiny
       unit test for it (fake anthropic client, fake execute_tool,
       verify `on_*` callbacks fire in the right order).
3. [ ] Migrate `RestAgent.ask()` to delegate to `run_tool_use_loop`.
       Run `pytest tests/test_rest_agent.py tests/test_tools.py -q`.
       Fix any failures before touching the MCP agent.
4. [ ] Update `mcp_agent.py` imports to point at `_claude_loop`. Drop
       the `from .rest_agent import ...` block. Run tests.
5. [ ] Migrate `McpAgent.ask()` to delegate to `run_tool_use_loop`.
       Run `pytest tests/test_mcp_agent.py -q`.
6. [ ] Create `tests/conftest.py` and move the shared stub helpers.
       Remove the duplicates from `test_rest_agent.py` and
       `test_mcp_agent.py`. Run full suite.
7. [ ] Apply the §5 boundary fixes (oauth docstring, `get_site_url`
       method, `.env.example` comment).
8. [ ] Run the full suite one final time: `python -m pytest tests/ -q`
       should print `77 passed` (or more, if new tests were added in §2).
9. [ ] Smoke-test REST mode against real Confluence with one real
       question (the most important regression check — the
       streaming/retry/tool-dispatch integration is the thing the
       tests mock).
10. [ ] Commit and push.

---

## Success criteria

- `pytest tests/ -q` prints at least `77 passed` with zero failures.
- `rg -n "from .rest_agent import" agent/mcp_agent.py` returns nothing.
- `rg -n "getattr\(.*_token" main.py` returns nothing.
- `wc -l agent/rest_agent.py agent/mcp_agent.py` shows both files
  smaller than today (today: ~410 and ~470 lines respectively).
- A diff of `RestAgent.ask` and `McpAgent.ask` is under ~25 lines each.
- The module docstring of `agent/oauth.py` no longer mentions MCP.
- A real REST-mode question still streams Markdown correctly and
  prints the `[Tools]` / `[Tokens]` footer.

---

## Estimated effort

~45 minutes of focused work. The test suite is strong enough that
most of that time is mechanical migration; the only real thinking is
the `run_tool_use_loop` signature design in step 2. If the suite
starts failing after step 3 or step 5, revert and re-stage smaller —
the issue is almost certainly a subtle difference in how `messages`
are appended or how `streamed_answer` is accumulated.
