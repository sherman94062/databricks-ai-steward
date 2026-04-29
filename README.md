# databricks-ai-steward

A governed [Model Context Protocol](https://modelcontextprotocol.io) server
that gives AI agents a safe, auditable interface to Databricks. Tools are
registered with a thin reliability layer (size cap, exception capture,
per-tool timeout, sync-tool rejection) so a misbehaving tool can't take down
the stdio session.

> **Status — governed gateway, live against a workspace.** The MCP server
> is wired to a live Databricks workspace. `execute_sql_safe` runs
> SELECT/EXPLAIN/SHOW/DESCRIBE statements through a sqlglot governance
> gate with row caps + statement-timeout cancellation; `list_catalogs`,
> `list_system_tables`, `recent_query_history`, `recent_audit_events`,
> and `billing_summary` are live system-table wrappers built on top of
> it. The remaining thin wrappers (`list_tables`, `describe_table`,
> `sample_table`) are scaffolding over `execute_sql_safe` — final SDK
> wiring, not new design work. The reliability layer (audit log,
> per-tool rate limit, caller-id propagation, graceful drain),
> production transports (HTTP + bearer auth + per-end-user identity),
> and stress / compatibility harness are built and exercised — see
> [`STRESS_FINDINGS.md`](STRESS_FINDINGS.md),
> [`COMPATIBILITY.md`](COMPATIBILITY.md), and [`SECURITY.md`](SECURITY.md).

---

## Two deployment shapes

The same codebase serves two very different audiences. Pick the one
that matches your situation.

### Shape A — local dev, Claude / Cursor / IDE clients

The MCP client (Claude Code, Claude Desktop, Cursor, Cline, etc.)
spawns the server as a **stdio subprocess**. There is no Docker, no
HTTP server, no port to expose. This is what you want if you're using
the steward to interact with Databricks from your own AI assistant.

```bash
source .venv/bin/activate
pip install -e '.[dev]'

# Auth — minimum required to call list_catalogs against a workspace
echo "DATABRICKS_HOST=https://<your-workspace>.cloud.databricks.com" >> .env
echo "DATABRICKS_TOKEN=dapi..." >> .env

# Smoke-test it directly
python -m mcp_server.server   # blocks waiting for an MCP client on stdio

# Or register with Claude Code
claude mcp add databricks-steward -- python -m mcp_server.server

pytest tests/
```

Per-client configuration recipes (Claude Desktop, Cursor, Goose,
LangChain) live in [`COMPATIBILITY.md`](COMPATIBILITY.md).

### Shape B — production HTTP service

A fintech that wants to expose this to many agents (or to a hosted
agent platform) deploys it as an HTTP service behind a reverse proxy
or ingress. The container ships with `streamable-http` as the default
transport and binds `0.0.0.0:8765`.

For Kubernetes, use the helm chart at
[`deploy/helm/databricks-ai-steward`](deploy/helm/databricks-ai-steward) —
ships `Deployment`, `Service`, `Ingress`, `NetworkPolicy`, `PDB`,
`HPA`, `ServiceMonitor`, and an optional log-shipper sidecar. For
single-host, [`deploy/docker-compose.yml`](deploy/docker-compose.yml)
brings up the same container with audit volume + healthcheck +
read-only rootfs.

```bash
# k8s
helm install steward ./deploy/helm/databricks-ai-steward \
  --set secrets.databricks.secretName=dbx-creds \
  --set secrets.bearer.secretName=steward-bearer

# raw docker
docker build -t databricks-ai-steward:dev .
docker run --rm -p 8765:8765 \
  -e DATABRICKS_HOST=https://<workspace>.cloud.databricks.com \
  -e DATABRICKS_TOKEN=dapi... \
  -e MCP_BEARER_TOKEN=$(openssl rand -hex 32) \
  -e MCP_BEARER_TOKEN_NAME=team-data \
  databricks-ai-steward:dev
```

Liveness / readiness endpoints for k8s (`GET /healthz`, `GET /readyz`)
are always reachable without auth so orchestrators can probe without
the bearer token. Everything else (`/mcp`, `/sse`) requires
`Authorization: Bearer <MCP_BEARER_TOKEN>`. See
[`SECURITY.md`](SECURITY.md) for the full env-var reference and
threat model.

Run the server *outside* a container the same way — just set
`--transport streamable-http` and pick a port:

```bash
python -m mcp_server.server --transport streamable-http --port 8765
```

### Transports

| Transport | Path | Used by |
|---|---|---|
| `stdio` (default) | n/a | Claude Code, Claude Desktop, Cursor (local), most IDE plugins |
| `streamable-http` | `/mcp` on `MCP_HOST:MCP_PORT` | Newer hosted agent harnesses; preferred for HTTP |
| `sse` | `/sse` + `/messages/` on `MCP_HOST:MCP_PORT` | Older HTTP-based clients still using SSE |

All three use the same tool surface, reliability guards, per-call
timeout, and cleanup-callback registry (via the FastMCP `lifespan` in
[`mcp_server/lifecycle.py`](mcp_server/lifecycle.py)). Stdio adds a
custom signal handler that closes stdin to defeat the anyio
reader-thread blocking issue; HTTP relies on uvicorn's built-in
graceful drain.

### HTTP security

By default HTTP transports bind only to `127.0.0.1` (loopback). To bind
externally you must pass `--allow-external` (or set
`MCP_ALLOW_EXTERNAL=1`) — the server refuses to start otherwise.

To require authentication, set `MCP_BEARER_TOKEN`. When set, every HTTP
request must carry `Authorization: Bearer <token>`; mismatches return
401 with timing-safe comparison. There is no other built-in auth — for
production exposure pair the token with TLS termination (reverse proxy
or sidecar). Verified by [`stress/probe_http_auth.py`](stress/probe_http_auth.py).

---

## Planned tool surface

| Tool | Status | Purpose |
|---|---|---|
| `list_catalogs` | live | Enumerate Unity Catalog catalogs (real `databricks-sdk` call) |
| `health` | live | Server liveness + drain-state introspection |
| `execute_sql_safe` | live | Run SELECT/EXPLAIN/SHOW/DESCRIBE against Databricks SQL with sqlglot governance + row cap + statement timeout |
| `list_system_tables` | live | Enumerate readable tables in the `system` catalog via `system.information_schema.tables` |
| `recent_audit_events` | live (warehouse-bound) | Recent rows from `system.access.audit`. May time out on small warehouses with high event volume — see tool docstring |
| `recent_query_history` | live | Recent rows from `system.query.history` |
| `billing_summary` | live | DBU consumption from `system.billing.usage`, grouped by SKU. With `MCP_DBU_RATE_CARD`, adds `cost_usd` per row + `total_usd` |
| `billing_report` | live | Stakeholder-friendly spend report: current + prior period + delta + monthly run-rate projection, plain-English labels |
| `list_tables` | planned | Thin wrapper over `execute_sql_safe` (`SHOW TABLES IN ...`) |
| `describe_table` | planned | Thin wrapper over `execute_sql_safe` (`DESCRIBE EXTENDED ...`) |
| `sample_table` | planned | Thin wrapper over `execute_sql_safe` (bounded `SELECT *`) |

Cross-cutting concerns under construction: SQL safety validation, schema
discovery, query governance policies, audit logging.

---

## Reliability layer

Every tool registered via `safe_tool()` (in `mcp_server/app.py`) gets:

1. **Exception capture** — uncaught exceptions become structured
   `{"error": {"type": ..., "message": ...}}` returns instead of killing
   the stdio process.
2. **Response size cap** (`MCP_MAX_RESPONSE_BYTES`, default 256 KB) —
   oversized payloads are replaced with a `ResponseTooLarge` error to
   protect the client's context window.
3. **Per-tool timeout** (`MCP_TOOL_TIMEOUT_S`, default 30 s) — async
   tools that exceed the cap are cancelled server-side and return a
   `ToolTimeout` error. This bounds resource leaks from cancelled
   client requests, since the MCP Python client SDK does not currently
   send `notifications/cancelled` upstream.
4. **Sync-tool rejection** — `safe_tool` raises `TypeError` on `def`
   tools by default. Sync tools own the asyncio event loop and break
   concurrency, shutdown, and signal handling on the stdio transport
   (see [`STRESS_FINDINGS.md`](STRESS_FINDINGS.md) §A1, B2, D). Pass
   `allow_sync=True` for fast pure-CPU work.
5. **Graceful restart** — `mcp_server/lifecycle.py` catches SIGTERM and
   SIGINT, cancels in-flight tool tasks within
   `MCP_SHUTDOWN_GRACE_S` (default 5 s), runs registered cleanup
   callbacks, and exits cleanly. Tools can register cleanup hooks via
   `lifecycle.register_cleanup(async_fn)` to release shared resources
   (DB connections, cursors). Verified by
   [`stress/probe_restart.py`](stress/probe_restart.py).
6. **Health/introspection tool** — `health` reports `{status, ready,
   version, uptime_s, in_flight_tasks}` and flips `ready` to false
   during shutdown so supervisors / probes can detect drain state.
7. **Audit log of every tool call** — JSONL records (`tool.start` +
   `tool.end` sharing a `request_id`) carry caller id, latency,
   outcome, error type, response bytes. Argument *values* are not
   logged (only names + a digest) so SQL bodies don't leak through
   the audit channel. Configurable file path via `MCP_AUDIT_LOG_PATH`;
   stderr by default.
8. **Per-(tool, caller) rate limit** — token bucket charged on entry.
   Default ceilings: `execute_sql_safe` 5/min, audit/billing
   tools 10/min, metadata 50/min. Override via `MCP_RATE_LIMIT`.
   Bounds the blast radius of a prompt-injected agent.
9. **Caller identity propagation to Databricks** — every Databricks
   statement is tagged with `mcp_caller=<id>`, visible in
   `system.query.history` and the workspace UI. Lets a human auditor
   attribute statements to the agent that triggered them, even though
   every statement runs under the same PAT.

---

## Stress testing

Two harnesses live under `stress/`:

- `stress/load.py` — concurrency / throughput baseline. Single session,
  configurable in-flight cap and total calls. Cleanly handled 126,000
  calls across c=1 to c=20,000 with zero errors.
- `stress/probe_*.py` — focused fault-injection probes. Each surfaces a
  specific real bug (sync-wedge, async-cancel leak, SIGINT mishandling,
  guard boundary divergence). All findings, root causes, and fixes are
  documented in [`STRESS_FINDINGS.md`](STRESS_FINDINGS.md).

```bash
# Throughput
python -m stress.load --concurrent 100 --total 5000

# Reliability probes
python -m stress.probe_a1_leak           # async cancellation leak
python -m stress.probe_a1_fix_verify     # confirm timeout fix
python -m stress.probe_d_blast_radius    # sync-tool blast radius
```

---

## Repository map

| Path | Purpose |
|---|---|
| `mcp_server/` | The MCP server itself: `app.py` (FastMCP instance + guards), `server.py` (entry point), `tools/`, `prompts.py`, `audit_verify.py` |
| `deploy/` | Helm chart (`helm/databricks-ai-steward/`) + `docker-compose.yml` |
| `stress/` | Load harness, fault-injection harness, cancellation/lifecycle probes |
| `tests/` | Unit tests for the reliability guards |
| `databricks/`, `governance/`, `agents/`, `examples/` | Empty placeholders for planned subsystems |
| [`CLAUDE.md`](CLAUDE.md) | Operating notes for Claude Code on this repo |
| [`WALKTHROUGH.md`](WALKTHROUGH.md) | Setup and tool-authoring guide |
| [`EXERCISE.md`](EXERCISE.md) | 9-phase hands-on guide that exercises every public surface (also as print-friendly [`docs/EXERCISE.html`](docs/EXERCISE.html)) |
| [`FLOW.md`](FLOW.md) | How requests move through the system |
| [`STRESS_FINDINGS.md`](STRESS_FINDINGS.md) | Empirical issues found via the stress harnesses, with reproductions and fixes |
| [`COMPATIBILITY.md`](COMPATIBILITY.md) | Client/framework compatibility matrix — what's been tested and what's pending |
| [`SECURITY.md`](SECURITY.md) | Threat model: what's in-scope, out-of-scope, known limitations, env-var reference |
| [`RUNBOOK.md`](RUNBOOK.md) | On-call operator's guide — deployment, troubleshooting, rotating credentials, scaling, rollback |
| [`INCIDENT_RESPONSE.md`](INCIDENT_RESPONSE.md) | Compromise playbooks — credential leak, anomalous query pattern, container escape, upstream CVE |
| [`COMPLIANCE.md`](COMPLIANCE.md) | SOC 2 / ISO 27001 control mapping + supply-chain posture + pre-deployment compliance checklist |
| [`AUDIT_LOG_SCHEMA.md`](AUDIT_LOG_SCHEMA.md) | Full audit-log schema, immutability + retention model, "what is not logged and why", verification recipes |
| [`FAILURE_MODES.md`](FAILURE_MODES.md) | Catalog of failure modes covered by the fault-injection harness |
| [`AGENTS.md`](AGENTS.md) | Original goal statement (pre-Claude Code) |
