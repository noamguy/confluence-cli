# Confluence CLI — Design & Analysis

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
