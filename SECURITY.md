# databricks-ai-steward — Threat Model

Explicit boundaries: what this server defends against today, what it
doesn't, and the test surface that proves the in-scope claims.

This is a *portfolio-stage* threat model — written when the server has
exactly one live tool (`list_catalogs`) plus introspection (`health`).
The model will tighten as the tool surface (and blast radius) grows.

---

## Trust boundaries

```
┌──────────────┐  MCP (stdio | streamable-http)  ┌────────────────┐
│  AI client   │ ──────────────────────────────► │  this server   │
│ (Claude,     │ ◄────────────────────────────── │  (one process) │
│  Cursor, …)  │                                 │                │
└──────────────┘                                 └───────┬────────┘
                                                         │
                                                         │ databricks-sdk
                                                         │ (HTTPS, PAT)
                                                         ▼
                                                ┌────────────────┐
                                                │   Databricks   │
                                                │   workspace    │
                                                └────────────────┘
```

The server runs as a *single trust principal* on behalf of the user. It
holds one Databricks PAT and uses it for every tool call. The server's
authority over Databricks equals the PAT's authority. There is no
per-tool sub-credentialing yet.

---

## In-scope (defended + tested)

| Threat | Defense | Test |
|---|---|---|
| Tool exception kills the server | `_guard` catches `Exception` → structured error | `test_guards.py::test_exception_becomes_structured_error` |
| Tool returns a payload that blows out the client's context | `MAX_RESPONSE_BYTES` cap (default 256 KB) → `ResponseTooLarge` | `test_guards.py::test_oversized_response_is_rejected` + `probe_e_boundary` |
| Tool returns something `json.dumps` can't serialize | `_cap_response` broad-except → `ResponseNotSerializable` | `test_guards.py::test_unserializable_response_is_rejected` + `probe_e_boundary` |
| Sync tool blocks the asyncio loop, wedging concurrent calls | `safe_tool` rejects `def` tools at registration | `STRESS_FINDINGS.md` §A1 / `probe_d_blast_radius` |
| Stuck tool leaks a server-side coroutine after client cancels | per-tool `asyncio.timeout`; `ToolTimeout` returned | `probe_a1_fix_verify` + filed upstream as [python-sdk#2507](https://github.com/modelcontextprotocol/python-sdk/issues/2507) |
| Slow shutdown leaks resources | Lifecycle handler (SIGTERM/SIGINT) with bounded grace + cleanup callbacks | `probe_restart` (stdio) + `probe_http_lifecycle` (HTTP) |
| Stdout pollution corrupts MCP transport | Logging pinned to stderr; SDK + middleware verified clean | `probe_stdout_clean` |
| Workspace URL or PAT leaks through error messages | `_scrub` replaces `DATABRICKS_HOST`, `DATABRICKS_TOKEN`, `MCP_BEARER_TOKEN` substrings before any error reaches the client | `test_databricks_faults.py` (host + token redaction tests) + `probe_token_audit` |
| HTTP transport reachable from other hosts on the network | Default bind = `127.0.0.1`; non-loopback bind requires `--allow-external` | `mcp_server/server.py:main` + manual verification |
| HTTP transport accepts unauthenticated calls | Optional `MCP_BEARER_TOKEN` → 401 on missing/wrong, timing-safe compare | `probe_http_auth` |
| SDK-level failure (401/403/429/500/timeout/network/malformed) crashes the server or leaks internals | All SDK exception types caught by `_guard`, message scrubbed, structured error returned | `test_databricks_faults.py` (parametrised over 10 failure modes) |
| File descriptor / memory leak under sustained load | httpx client pool reuse via SDK singleton + `asyncio.to_thread` worker pool | `probe_databricks_soak` (1000 calls: fd + 0, RSS + 0.1 MB) |

---

## Out of scope (explicitly)

| Threat | Why out of scope today |
|---|---|
| **Prompt-injected LLM client enumerates Databricks** | A compromised AI client can drive the server to call any tool. The server is a faithful executor — preventing the client from being adversarial is the client's job, not ours. Mitigation lives in the planned `governance/` layer (per-tool ACLs, audit log, rate limit). |
| **OS-level container / sandbox escape** | Server runs with the user's full shell privileges. Sandboxing belongs to the deployment, not the application. |
| **PAT theft from disk** | `.env` is gitignored + chmod 600, but anyone with read access to the user's home directory can read it. Token rotation is the mitigation. |
| **HTTPS termination / mTLS** | HTTP transport intentionally serves plain HTTP. Production deployment expects a reverse proxy or sidecar to terminate TLS. |
| **Per-tool sub-credentials** | One PAT shared across all tools. When `execute_sql_safe` lands the steward should be able to issue a more limited token per-call — not yet implemented. |
| **Audit logging of every tool call** | Currently no persistent log of *which agent invoked what tool with what args*. Planned in the `governance/` slice; until then, supervisors must rely on Databricks-side audit logs. |
| **Supply-chain attack on `databricks-sdk` / `mcp` / `uvicorn`** | We pin floors but not ceilings. Re-evaluate if any of these dependencies have a known compromise. |
| **Long-running soak (hours)** | The 1000-call soak runs ~5 minutes. Memory growth at hour-scale is unverified. Run again before any production deployment. |

---

## Known limitations (today)

1. **Single PAT, no per-tool ACL.** Every tool inherits the workspace
   user's full permissions. Once `execute_sql_safe` lands, this becomes
   the load-bearing question.
2. **No persistent audit trail.** The structured stderr log captures
   warnings + errors but not full call history. `mcp_server.app.log`
   inherits the level from `MCP_LOG_LEVEL` and writes to stderr only.
3. **Bearer auth is bring-your-own.** No built-in identity provider, no
   token rotation, no per-caller scope. Pair with a reverse proxy for
   anything beyond loopback dev use.
4. **Cancellation propagation depends on local fix.** Until
   [python-sdk#2507](https://github.com/modelcontextprotocol/python-sdk/issues/2507)
   ships, the server-side per-tool timeout is the only thing bounding
   leak windows. Tune `MCP_TOOL_TIMEOUT_S` to match your tools' real
   p99.
5. **No DoS rate limit.** A misbehaving client can spam tool calls up
   to the workspace's API rate limit, then start eating 429s. Cost-side
   risk is bounded by the workspace's quota, not by us.

---

## Configuration reference

| Variable | Purpose | Default |
|---|---|---|
| `DATABRICKS_HOST` | Workspace base URL | (none — required) |
| `DATABRICKS_TOKEN` | PAT (stays in `.env`, scrubbed from error messages) | (none — required) |
| `MCP_BEARER_TOKEN` | If set, HTTP transports require `Authorization: Bearer <value>` | unset |
| `MCP_ALLOW_EXTERNAL` | If `1`, HTTP transports may bind non-loopback hosts | unset |
| `MCP_HOST` | Bind host for HTTP transports | `127.0.0.1` |
| `MCP_PORT` | Bind port for HTTP transports | `8765` |
| `MCP_MAX_RESPONSE_BYTES` | Cap on tool response size | `262144` (256 KB) |
| `MCP_TOOL_TIMEOUT_S` | Per-tool server-side timeout | `30` |
| `MCP_SHUTDOWN_GRACE_S` | Stdio graceful shutdown deadline | `5` |
| `MCP_CLEANUP_TIMEOUT_S` | Per cleanup-callback deadline | `2` |
| `MCP_LOG_LEVEL` | Logger level (stderr only) | `WARNING` |

---

## Reporting a security issue

Open an issue in this repository and apply the `security` label. Don't
include exploitation details in the public title — open with a brief
description, then add specifics in a reply once the maintainer
acknowledges.

The issue tracker is https://github.com/sherman94062/databricks-ai-steward/issues.
