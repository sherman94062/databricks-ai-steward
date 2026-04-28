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
| Caller submits DML/DDL/multi-statement SQL | `mcp_server/databricks/sql_safety.py` parses with sqlglot and rejects everything that isn't SELECT/EXPLAIN/SHOW/DESCRIBE before any workspace contact | `test_sql_safety.py` (~25 cases) + `test_sql_tools.py` (mocked SDK) + `probe_sql_governance` (live workspace, tripwire confirms 0 SDK calls for 13 forbidden statement classes) |
| Caller asks for unbounded rows | `MCP_SQL_ROW_LIMIT` (default 100) and `MCP_SQL_HARD_ROW_LIMIT` (default 1000) cap server-side via the SDK's `row_limit` parameter; response carries `truncated` when the cap fired | `test_sql_tools.py::test_row_limit_capped_at_hard_ceiling` |
| Slow query holds the session forever | `MCP_SQL_WAIT_TIMEOUT_S` (default 25 s, capped at Databricks' 50 s ceiling) caps the total wait inside `execute_sql_safe`. After the 5 s SDK-minimum initial submit, we poll `get_statement` and call `cancel_execution(statement_id)` if the budget runs out. Outer per-tool `MCP_TOOL_TIMEOUT_S` is the second gate. | `probe_sql_concurrency` |
| Cancelled tool call leaves the Databricks query running | `_execute_with_cancellation` catches every cancellation path (asyncio.CancelledError from external cancel, KeyboardInterrupt, SDK errors) and calls `cancel_execution(statement_id)` before re-raising. The submit-then-poll pattern is what makes the asyncio cancellation point reachable — the previous synchronous `wait_timeout=25s, on_wait_timeout=CANCEL` pattern kept the worker thread alive until the SDK's own timer fired, burning warehouse compute on requests nobody was listening to. | `test_sql_tools.py::test_cancellation_calls_cancel_execution_at_databricks` (mocked) + `probe_sql_cancellation.py` (live, ~10s round-trip ending in `state=CANCELED` after asyncio cancel at t=8s) |
| System-table tools leak data the caller's PAT shouldn't see | `list_system_tables` / `recent_audit_events` / `recent_query_history` / `billing_summary` use `system.information_schema.tables` and rely on Unity Catalog grants — invisible schemas just don't appear. Same per-tool 60 s ceiling. Args are int-validated and clamped before SQL construction. | `test_system_table_tools.py` (arg clamping + passthrough error mapping + reshape) |
| No persistent record of who called what | Every tool call emits a JSONL audit pair (`tool.start` + `tool.end`) sharing a `request_id`. Records carry caller identity, latency, outcome, error type, response bytes. Argument *values* are not logged (only names + a digest) to avoid exfiltrating SQL bodies or table FQNs through the audit channel. Configurable via `MCP_AUDIT_LOG_PATH`. | `test_audit_and_rate_limit.py` (start/end pairing, error outcome, value redaction, caller_id propagation) |
| Compromised / prompt-injected agent enumerates the workspace | Per-(tool, caller) sliding-window rate limiter charged on entry, not completion. Defaults: `execute_sql_safe` 5/min, audit/billing/query-history tools 10/min, metadata 50/min. Override via `MCP_RATE_LIMIT`. | `test_audit_and_rate_limit.py::test_rate_limit_*` (refusal after quota, per-caller isolation, charges on tool failure) |
| Databricks audit trail can't attribute statements to the originating agent | `execute_sql_safe` (and the system-table wrappers, which route through it) tags every Databricks statement with `query_tags=[mcp_caller=<id>, mcp_source=databricks-ai-steward]`. The caller id propagates to the workspace's own `system.query.history` and the SQL warehouse UI. | `test_sql_tools.py::test_caller_id_propagates_to_databricks_query_tags` |

---

## Out of scope (explicitly)

| Threat | Why out of scope today |
|---|---|
| **Prompt-injected LLM client enumerates Databricks** | A compromised AI client can drive the server to call any tool. Two of the three mitigations are now in place: per-(tool, caller) rate limiter (default 5/min for `execute_sql_safe`) bounds the volume; per-call audit log captures every attempt for after-the-fact forensics. The third — per-tool sub-credentials — is still future work. The server is a faithful executor; preventing the client from being adversarial in the first place is the client's job. |
| **OS-level container / sandbox escape** | Server runs with the user's full shell privileges. Sandboxing belongs to the deployment, not the application. |
| **PAT theft from disk** | `.env` is gitignored + chmod 600, but anyone with read access to the user's home directory can read it. Token rotation is the mitigation. |
| **HTTPS termination / mTLS** | HTTP transport intentionally serves plain HTTP. Production deployment expects a reverse proxy or sidecar to terminate TLS. |
| **Per-tool sub-credentials** | One PAT shared across all tools. When `execute_sql_safe` lands the steward should be able to issue a more limited token per-call — not yet implemented. |
| ~~**Audit logging of every tool call**~~ — **shipped.** | JSONL via `MCP_AUDIT_LOG_PATH` (or stderr). Argument *values* are deliberately excluded — only argument names + a digest are recorded. Full-fidelity forensic replay still needs Databricks-side audit. |
| **Supply-chain attack on `databricks-sdk` / `mcp` / `uvicorn`** | We pin floors but not ceilings. Re-evaluate if any of these dependencies have a known compromise. |
| **Long-running soak (hours)** | The 1000-call soak runs ~5 minutes. Memory growth at hour-scale is unverified. Run again before any production deployment. |
| ~~**Server-side rate limit / quota enforcement**~~ — **shipped.** | In-process token-bucket per `(tool, caller)`. Defaults: `execute_sql_safe` 5/min, audit/billing/query-history 10/min, metadata 50/min. Override via `MCP_RATE_LIMIT="tool=N/W,*=N/W"`. Multi-replica deployments would need a shared backend (Redis); the surface stays the same. |

---

## Known limitations (today)

1. **Single PAT, no per-tool ACL.** Every tool inherits the workspace
   user's full permissions. Once `execute_sql_safe` lands, this becomes
   the load-bearing question.
2. **Audit log is per-process.** Each MCP server subprocess writes its
   own JSONL file (or stderr stream). Multi-replica HTTP deployments
   need a log shipper (Vector / Filebeat / Fluent Bit) to aggregate;
   the records are already in a JSONL shape that's friendly to those.
   Argument *values* are deliberately not logged — only names + a
   short digest — to avoid exfiltrating SQL bodies, table FQNs, and
   other workspace internals through the audit channel itself. Full
   forensic replay still needs Databricks-side audit
   (`system.access.audit` and `system.query.history`).
3. **Bearer auth is bring-your-own.** No built-in identity provider, no
   token rotation, no per-caller scope. Pair with a reverse proxy for
   anything beyond loopback dev use.

   **Per-end-user identity gap.** When `MCP_BEARER_TOKEN` is set, every
   authenticated request gets `caller_id = MCP_BEARER_TOKEN_NAME` —
   *bearer-token-level* attribution, not per-end-user. If multiple
   humans share a single bearer token (typical for a shared-deployment
   model where the platform owns the steward and individual analysts
   hit it through an agent), the audit log + Databricks `query_tags`
   cannot tell them apart. Closing this gap requires either:
   - an OAuth-per-user flow at the HTTP layer, or
   - a trust relationship with an upstream proxy that propagates a
     header like `X-End-User: <id>` which the bearer middleware
     reads into the `caller_id` contextvar in addition to the
     bearer-token name.

   Both approaches are middleware additions (no SDK or transport
   changes) — picking which depends on how Drew's identity story
   shakes out.
4. **Cancellation propagation depends on local fix.** Until
   [python-sdk#2507](https://github.com/modelcontextprotocol/python-sdk/issues/2507)
   ships, the server-side per-tool timeout is the only thing bounding
   leak windows. Tune `MCP_TOOL_TIMEOUT_S` to match your tools' real
   p99.
5. **`recent_audit_events` is warehouse-bound.** On Databricks Free
   Edition with a 2X-Small Serverless Starter, the audit table query
   reliably exceeds Databricks' 50 s synchronous-execution ceiling
   even with partition pruning by `event_date`, returning
   `StatementFailed: CANCELED`. The other system-table tools
   (`list_system_tables`, `recent_query_history`, `billing_summary`)
   work fine on the same warehouse. Workarounds: tighten predicates
   via `execute_sql_safe` directly (e.g.
   `WHERE service_name = 'unityCatalog'`), or upgrade the warehouse.
   Workaround does not change the threat model — same governance
   gate, same row caps.

6. **Concurrent SQL grows the connection pool.** `probe_sql_concurrency`
   shows ~2 fds and ~3 MB RSS per concurrent in-flight `execute_sql_safe`
   call (httpx pool growing to support per-statement HTTPS streams).
   Bounded — not a leak — but means heavy SQL concurrency under a single
   server process scales by client connections, not by request count.
   Worth knowing if you ever expose this server to many simultaneous
   agents.

7. **Warehouse-level isolation is a deployment choice, not a code
   constraint.** `MCP_DATABRICKS_WAREHOUSE_ID` (or the
   resolver's "first running" fallback) points the steward at *a*
   warehouse — but doesn't enforce that the warehouse has narrow
   grants. For production at a regulated workplace, the recommended
   pattern is:

   - one *dedicated* SQL warehouse for the steward, sized for
     interactive analytics workloads (not training/ETL)
   - the steward's service principal has `USE_CATALOG` / `USE_SCHEMA`
     / `SELECT` only on non-PII tables in that warehouse's UC scope
   - sensitive payments / KYC / PCI tables live under catalogs the
     steward's SP has no grant on — Databricks rejects the query
     before the steward's governance gate even sees it

   The per-tool credential abstraction (`MCP_TOOL_TOKEN_<TOOL>`) is
   the runtime hook that lets each tool use a *different* SP if the
   "one isolated warehouse" model is too coarse.

8. **No server-side rate limit.** A misbehaving client can spam tool
   calls up to the workspace's API rate limit, then start eating 429s.
   What does happen for free, before our code sees the error:
   - **Databricks** returns `429 Too Many Requests` with a
     `Retry-After` header when limits are hit. Per-endpoint and
     per-plan; not centrally documented.
   - **`databricks-sdk`** has a `_RetryAfterCustomizer` that respects
     `Retry-After` and auto-retries idempotent operations (visible at
     `databricks/sdk/errors/customizer.py` in the installed SDK). So a
     transient burst usually self-heals before reaching us.
   - **This server** surfaces persistent 429s (after the SDK gives up)
     as a structured `TooManyRequests` error via `_guard`. Covered by
     `test_databricks_faults.py::test_sdk_error_becomes_structured_response[429_rate_limit]`.

   What we don't have: a *server-side* token bucket that protects
   workspace quota (and SQL-warehouse cost) when `execute_sql_safe`
   eventually lands. Tracked under "Out of scope" with an explicit
   reactivation trigger.

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
| `MCP_DATABRICKS_WAREHOUSE_ID` | Default SQL warehouse for `execute_sql_safe`. Falls back to first running warehouse if unset. | unset |
| `MCP_SQL_ROW_LIMIT` | Default per-call row cap for `execute_sql_safe` | `100` |
| `MCP_SQL_HARD_ROW_LIMIT` | Hard ceiling — caller-supplied `row_limit` is silently capped to this | `1000` |
| `MCP_SQL_WAIT_TIMEOUT_S` | Server-side `wait_timeout` passed to the Databricks Statement Execution API; `on_wait_timeout=CANCEL` aborts the statement at this deadline | `25` |
| `MCP_AUDIT_LOG_PATH` | Append-only JSONL audit log path. If unset, audit goes to stderr only. | unset |
| `MCP_AUDIT_DISABLE_STDERR` | If `1`, suppress audit-log emission to stderr (file write still happens). Useful when a sidecar log shipper is reading the JSONL file. | unset |
| `MCP_CALLER_ID` | Default caller identity for this process. Overridden per-request by transport-layer hooks (e.g. bearer-auth middleware). | `unknown` |
| `MCP_BEARER_TOKEN_NAME` | Caller identity used in audit + Databricks `query_tags` when a request authenticates with `MCP_BEARER_TOKEN`. | `bearer-authenticated` |
| `MCP_RATE_LIMIT` | Per-tool overrides, e.g. `execute_sql_safe=10/60,*=200/60`. Each entry is `tool=count/window_seconds`; `*` matches any tool not explicitly listed. | (defaults applied) |

## k8s integration

HTTP transports expose two unauthenticated probe endpoints. Both
return plain-text bodies so they're easy to read in `kubectl describe`:

| Endpoint | Returns | Probe type |
|---|---|---|
| `GET /healthz` | `200 ok` while the process is alive | `livenessProbe` |
| `GET /readyz` | `200 ready` normally; `503 draining` once shutdown is signalled | `readinessProbe` |

The Dockerfile's `HEALTHCHECK` directive uses `/healthz` so
docker-managed restarts work the same way k8s does. Both probes
bypass `MCP_BEARER_TOKEN` — orchestrators don't have the token, and
exposing liveness state is not a meaningful information leak.

Sample manifest snippet:

```yaml
livenessProbe:
  httpGet: {path: /healthz, port: 8765}
  periodSeconds: 30
readinessProbe:
  httpGet: {path: /readyz, port: 8765}
  periodSeconds: 5
```
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
