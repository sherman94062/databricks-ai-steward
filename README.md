# databricks-ai-steward

A governed [Model Context Protocol](https://modelcontextprotocol.io) server
that gives AI agents a safe, auditable interface to Databricks. Tools are
registered with a thin reliability layer (size cap, exception capture,
per-tool timeout, sync-tool rejection) so a misbehaving tool can't take down
the stdio session.

> **Status — early integration.** The MCP server is wired to a live
> Databricks workspace: `list_catalogs` calls the Unity Catalog API
> (`databricks-sdk`) and returns real catalog metadata. The remaining
> planned tools (`list_tables`, `describe_table`, `sample_table`,
> `execute_sql_safe`) are still designed-but-not-implemented. The
> reliability layer and the stress / compatibility harness are built and
> exercised — see [`STRESS_FINDINGS.md`](STRESS_FINDINGS.md) and
> [`COMPATIBILITY.md`](COMPATIBILITY.md).

---

## Quick start

```bash
source .venv/bin/activate
pip install -e '.[dev]'             # add ',http' for HTTP transports

# Auth — minimum required to call list_catalogs against a workspace
echo "DATABRICKS_HOST=https://<your-workspace>.cloud.databricks.com" >> .env
echo "DATABRICKS_TOKEN=dapi..." >> .env

# Run the server — choose a transport
python -m mcp_server.server                              # stdio (default)
python -m mcp_server.server --transport streamable-http  # http://127.0.0.1:8765/mcp
python -m mcp_server.server --transport sse              # http://127.0.0.1:8765/sse

# Register with Claude Code (stdio)
claude mcp add databricks-steward -- python -m mcp_server.server

# Tests
pytest tests/
```

See [`WALKTHROUGH.md`](WALKTHROUGH.md) for the full setup and
tool-authoring guide.

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
| `list_tables` | planned | Enumerate tables in a catalog / schema |
| `describe_table` | planned | Return column definitions and metadata |
| `sample_table` | planned | Return a bounded row sample |
| `execute_sql_safe` | planned | Run SQL with governance checks (SELECT-only, row caps, PII guards) |

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
| `mcp_server/` | The MCP server itself: `app.py` (FastMCP instance + guards), `server.py` (entry point), `tools/` |
| `stress/` | Load harness, fault-injection harness, cancellation/lifecycle probes |
| `tests/` | Unit tests for the reliability guards |
| `databricks/`, `governance/`, `agents/`, `examples/` | Empty placeholders for planned subsystems |
| [`CLAUDE.md`](CLAUDE.md) | Operating notes for Claude Code on this repo |
| [`WALKTHROUGH.md`](WALKTHROUGH.md) | Setup and tool-authoring guide |
| [`FLOW.md`](FLOW.md) | How requests move through the system |
| [`STRESS_FINDINGS.md`](STRESS_FINDINGS.md) | Empirical issues found via the stress harnesses, with reproductions and fixes |
| [`COMPATIBILITY.md`](COMPATIBILITY.md) | Client/framework compatibility matrix — what's been tested and what's pending |
| [`SECURITY.md`](SECURITY.md) | Threat model: what's in-scope, out-of-scope, known limitations, env-var reference |
| [`FAILURE_MODES.md`](FAILURE_MODES.md) | Catalog of failure modes covered by the fault-injection harness |
| [`AGENTS.md`](AGENTS.md) | Original goal statement (pre-Claude Code) |
