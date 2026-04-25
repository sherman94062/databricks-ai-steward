# databricks-ai-steward

A governed [Model Context Protocol](https://modelcontextprotocol.io) server
that gives AI agents a safe, auditable interface to Databricks. Tools are
registered with a thin reliability layer (size cap, exception capture,
per-tool timeout, sync-tool rejection) so a misbehaving tool can't take down
the stdio session.

> **Status — scaffolding.** The MCP server runs and exposes one stub tool
> (`list_catalogs`). The Databricks client, SQL safety layer, and audit
> logging are designed but not yet implemented. The reliability layer and
> the stress-testing harness *are* built and exercised — see
> [`STRESS_FINDINGS.md`](STRESS_FINDINGS.md).

---

## Quick start

```bash
source .venv/bin/activate
pip install -e '.[dev]'

# Run the server (stdio — blocks waiting for an MCP client)
python -m mcp_server.server

# Or register with Claude Code
claude mcp add databricks-steward -- python -m mcp_server.server

# Tests
pytest tests/
```

See [`WALKTHROUGH.md`](WALKTHROUGH.md) for the full setup and
tool-authoring guide.

---

## Planned tool surface

| Tool | Status | Purpose |
|---|---|---|
| `list_catalogs` | stub | Enumerate Unity Catalog catalogs |
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
| [`FAILURE_MODES.md`](FAILURE_MODES.md) | Catalog of failure modes covered by the fault-injection harness |
| [`AGENTS.md`](AGENTS.md) | Original goal statement (pre-Claude Code) |
