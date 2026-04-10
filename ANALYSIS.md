# Confluence CLI — Design & Analysis

## TL;DR

The project ships two Confluence backends behind one CLI so they can be compared head-to-head on the same question: **REST** (direct Confluence REST API with our own OAuth token) and **MCP** (the official Atlassian MCP server). After running both in production for real queries, every empirical data point pointed in the same direction:

- **MCP mode does not accept standard OAuth bearer tokens.** The hosted `mcp.atlassian.com` server rejects the Atlassian 3LO token we issue for REST mode. The only supported client is [`mcp-remote`](https://www.npmjs.com/package/mcp-remote), a Node.js package that runs as a local stdio proxy and manages its own separate browser auth under `~/.mcp-auth/`. This silently makes Node.js v18+ a runtime dependency and forces users through **two independent auth flows** with two different credential stores we cannot introspect or refresh from Python.
- **MCP mode is mathematically unusable on the default Anthropic rate-limit tier.** The server injects ~24,000 input tokens of tool schemas on *every* Claude call. At the default 10,000 input-tokens-per-minute budget, a single interactive question is guaranteed to 429. REST mode sends ~2k tokens of schemas and stays well under the budget.
- **MCP mode hides its own failures.** When the Atlassian backend is degraded it returns `CallToolResult` with `isError=true` and a human-readable error message as `content`, so Claude reads the error as data and paraphrases it back to the user as a polite apology — making root-cause analysis impossible without manual `isError` checking and `BaseExceptionGroup` unwrapping.
- **REST mode worked first-try and has worked ever since.** The direct `/wiki/api/v2/pages/{id}` path plus a small custom tool surface gives us full control over scopes, token lifecycle, error surfaces, and token footprint. It's the correct choice for any production agent, and the project's comparison table is essentially a formal statement of that conclusion.

MCP mode remains in the codebase as a working reference implementation so the comparison is grounded in real code, not just theory. If you're building on top of Atlassian's data, **use the REST path.**

---

## Approach

When implementing a new feature I start with the requirements, then investigate available solutions before writing a single line of code.

**Requirements:**
- Accept a natural language question from a user
- Answer the question based on relevant content from a Confluence workspace
- Use Claude Sonnet 4.6 as the LLM
- Use the official Atlassian MCP server as the Confluence data source
- Authentication must use OAuth, not API keys

Since the official Atlassian MCP server was explicitly required, I started by investigating its limitations, repository health (open issues, PR activity, update frequency), and real-world usage reports before deciding on an implementation strategy.

---

## Official Atlassian MCP Server — Investigation

### Repository Health
- 50+ open issues, with new ones filed daily
- Known disconnection issues reported across multiple clients (VS Code, GitHub Copilot)
- `/sse` endpoint being deprecated — killed after June 30, 2026

### Key Limitation: Cloud Only
The biggest limitation is that the official MCP server **only works with Atlassian Cloud**. On-prem (Data Center / Server) deployments are completely unsupported.

For a production agent this is a deal-breaker: many enterprises run Jira and Confluence on-prem for security and compliance reasons. An agent that only serves Cloud customers is not a general solution.

---

## Approach Comparison

| | Official Atlassian MCP | Direct REST API |
|---|---|---|
| **Deployment** | Cloud only | Cloud + On-Prem |
| **Auth** | OAuth 2.1 (browser JIT) or API token (admin must enable) | OAuth 2.1 (same token, no admin dependency) |
| **Token usage** | ~24k tokens per query (full schema injected) | Minimal (only your tool definitions) |
| **Reliability** | Frequent disconnections, `invalid_token` degradation | Stable HTTP |
| **Confluence toolset** | 11 tools, no attachments/bulk/history | Full API surface — you define what you need |
| **Jira write ops** | Partial/broken (transitions, assignments, epic links missing) | Full REST API coverage |
| **On-prem support** | ❌ | ✅ |
| **SSE deprecation risk** | `/sse` killed June 30 2026 | N/A |
| **Setup complexity** | MCP proxy + OAuth wizard + Node.js dependency | Plain HTTP + OAuth token |
| **Debugging** | Opaque (MCP protocol layer) | Transparent (standard HTTP logs) |

---

## Conclusions

The official Atlassian MCP server is a reasonable first step for IDE-integrated, single-user, Cloud-only workflows. However, for production agents it introduces unnecessary complexity, reliability risks, and token costs.

The direct REST approach delivers the same capabilities — using the **identical OAuth token** — with better reliability, lower cost, full API coverage, and no dependency on Atlassian's hosted MCP infrastructure.

**MCP makes sense when:** you want zero integration code and you're already in a supported IDE (Claude Desktop, Cursor).

**REST makes sense when:** you're building an agent, need reliability, care about token costs, or serve on-prem customers.

---

## Implementation Notes

### Why REST Mode Felt Slow (and How I Fixed It)

REST mode is fast per individual call, but executes multiple sequential Claude round-trips:

1. User asks a question → Claude responds with `tool_use: confluence_search(...)`
2. We hit Confluence → feed results back → Claude responds with `tool_use: get_page(id=...)`
3. We hit Confluence → feed the full page body back → Claude writes the final answer

A typical question involves **3 Claude calls + 2 Confluence calls**, and without streaming the user sees nothing until the very last token arrives.

**Fix:** Switched from `messages.create(...)` to `messages.stream(...)` so tokens print as they arrive. This is the biggest perceived-latency win — first token appears in well under a second instead of waiting for the full answer. A lightweight `thinking...` indicator between tool calls ensures the REPL is never silently waiting.

### MCP Mode Instability

In MCP mode, errors surface as cryptic Claude responses rather than explicit error messages. This is a hallmark symptom of the MCP layer's instability — and the irony is that the error gets hidden by Claude rather than surfaced, which is the opposite of what you want when debugging a production agent.

The REST approach fails loudly and explicitly with standard HTTP status codes, making issues immediately actionable.

---

### The MCP Server Does Not Accept Standard OAuth Bearer Tokens

The most significant finding of this project. The first MCP-mode implementation connected directly to `https://mcp.atlassian.com/v1/mcp` over streamable HTTP and passed our Atlassian OAuth 2.0 (3LO) access token as an `Authorization: Bearer <token>` header — the natural pattern, and the one that works for every other Atlassian REST endpoint routed through `api.atlassian.com`.

**It doesn't work for the MCP server.** Every tool call came back with either a TaskGroup exception wrapping an auth-layer failure, or a `CallToolResult` with `isError=true` and the payload `{"error":true,"message":"We are having trouble completing this action. Please try again shortly."}` — even on tools that don't touch Confluence content at all, like `atlassianUserInfo` and `getAccessibleAtlassianResources`. Expanding our OAuth scopes to match the exact consent screen Claude chat shows (including granular `read:page:confluence`, `write:comment:confluence`, `read:me`, `read:account`, etc.) did not help. The MCP server consistently refused tool calls regardless of the token's scope footprint.

The supported path — not advertised anywhere in the MCP server's own docs and discovered only by tracing how `claude.ai` itself connects — is to use [`mcp-remote`](https://www.npmjs.com/package/mcp-remote): a Node.js package that runs locally as an `stdio` MCP proxy, opens its own browser window on first run, caches credentials under `~/.mcp-auth/`, and forwards JSON-RPC between our Python process and Atlassian's hosted server.

**Consequences for anyone building on top of the official Atlassian MCP server:**

1. **Node.js becomes a runtime dependency.** A Python agent that "uses Atlassian MCP" is silently also a Node.js deployment, because the only supported client is an `npm` package spawned via `npx`.

2. **MCP mode and REST mode cannot share an OAuth token.** The REST path uses our own `agent/oauth.py` token at `~/.confluence-cli/token.json`; the MCP path uses whatever mcp-remote caches at `~/.mcp-auth/`. The user must authenticate **twice**, through two completely different browser flows, granting two overlapping-but-distinct scope sets. A CLI `--reset` flag can only reset the REST token; the mcp-remote credentials are opaque to us and require deleting a directory by hand.

3. **The auth lifecycle is uncontrollable from Python.** We cannot inspect the mcp-remote token, refresh it on a schedule, rotate it when scopes change, or bundle fresh credentials into a test fixture. The agent has no visibility into whether auth is healthy until it fails.

4. **The failure mode is dishonest by default.** Because the MCP server returns errors as `CallToolResult.content` text rather than surfacing them at the transport layer, Claude reads them as data and paraphrases them into polite apologies — silently hiding the real cause from the user. Our implementation mitigates this by checking `result.isError`, unwrapping `BaseExceptionGroup` from the `anyio` transport, and logging the raw error to stderr, but these are workarounds for a client/server contract that should never have surfaced errors this way in the first place.

This is a fundamental architectural limitation of the official MCP server, not a bug we can fix in our client. It reinforces every conclusion in the comparison table above: the direct REST approach gives us a single OAuth flow we control end-to-end, no Node.js dependency, transparent HTTP-level errors, and auth state that lives in one place under our management. For any use case beyond a single developer pointing a supported IDE at the MCP server, REST is the correct choice.
