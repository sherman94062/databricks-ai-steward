# databricks-ai-steward — Walkthrough

A setup-and-tool-authoring guide for the Databricks AI Steward MCP
server. For a hands-on tour of every shipped feature once setup is
done, see [`EXERCISE.md`](EXERCISE.md).

> **Current state.** Production-grade. `execute_sql_safe` runs
> SELECT/EXPLAIN/SHOW/DESCRIBE statements through a sqlglot
> governance gate against a live Databricks workspace.
> `list_catalogs`, `health`, `list_system_tables`,
> `recent_query_history`, `recent_audit_events`, and
> `billing_summary` are live. Reliability layer (audit log with hash
> chain, per-tool rate limit, caller-id propagation, graceful drain),
> production transports (HTTP + bearer auth + per-end-user identity),
> deployment artifacts (Dockerfile + helm chart + docker-compose),
> and CI (pytest + ruff + mypy + bandit + pip-audit + Trivy + helm
> lint + kubeconform) are all in place.

---

## 1. What this project is

An MCP server that gives AI agents a **governed** interface to
Databricks. The intent is that any MCP client (Claude Code, Claude
Desktop, Cursor, Goose, LangChain, etc.) cannot touch Databricks
except through this server, and every tool the server exposes
enforces safety rules, logs the call, and returns structured
results.

Live tools today:

| Tool | Purpose |
|---|---|
| `list_catalogs` | Enumerate Unity Catalog catalogs the configured workspace can see |
| `health` | Server liveness + drain-state introspection |
| `execute_sql_safe` | Run SELECT/EXPLAIN/SHOW/DESCRIBE through sqlglot governance + row caps + statement timeout |
| `list_system_tables` | Enumerate readable tables in the `system` catalog |
| `recent_query_history` | Recent rows from `system.query.history` |
| `recent_audit_events` | Recent rows from `system.access.audit` (warehouse-bound; see RUNBOOK) |
| `billing_summary` | DBU consumption from `system.billing.usage`, grouped by SKU |

Planned thin wrappers around `execute_sql_safe`: `list_tables`,
`describe_table`, `sample_table`. Final SDK wiring, not new design.

---

## 2. Prerequisites

- Python 3.12+ (repo is developed on 3.14; pinned in `pyproject.toml`)
- A Databricks workspace URL + personal access token
- An MCP-compatible client (Claude Code, Claude Desktop, Cursor,
  MCP Inspector, etc.) to exercise the server end-to-end

---

## 3. Install

```bash
cd <repo-root>
source .venv/bin/activate
pip install -e '.[dev,http]'
```

`.[dev]` pulls test/lint/security tooling. `.[http]` adds uvicorn +
starlette for the HTTP transports. Drop `[dev,http]` for a
stdio-only runtime install.

Configure workspace credentials in `.env`:

```bash
echo "DATABRICKS_HOST=https://<workspace>.cloud.databricks.com" >> .env
echo "DATABRICKS_TOKEN=dapi..." >> .env
```

`mcp_server.server` calls `load_dotenv()` at startup, so any env-var
documented in `SECURITY.md` "Configuration reference" can live in
`.env`.

---

## 4. Run the server standalone

Stdio (default — what most MCP clients want):

```bash
python -m mcp_server.server
# Blocks waiting for an MCP client on stdin. Silence is correct;
# anything written to stdout that isn't JSON-RPC corrupts the
# protocol. Logs go to stderr at WARNING level.
```

HTTP (for hosted agent harnesses, or when you want a visible
startup banner):

```bash
python -m mcp_server.server --transport streamable-http --port 8765
# Uvicorn prints "Started server process [pid]" + "Running on ..."
```

Sanity-check tools are registered without sitting through a
full MCP handshake:

```bash
python -c "
import asyncio
from mcp_server.app import mcp
import mcp_server.tools.basic_tools  # noqa: F401
import mcp_server.tools.health  # noqa: F401
import mcp_server.tools.sql_tools  # noqa: F401
print(sorted(t.name for t in asyncio.run(mcp.list_tools())))
"
```

---

## 5. Register with Claude Code

```bash
claude mcp add databricks-steward -- python -m mcp_server.server
```

After this, Claude Code will spawn the server as a stdio subprocess
on each session. Prompt Claude with something like *"list the
databricks catalogs"* and it will invoke `list_catalogs`. Real
Unity Catalog data comes back as `{"catalogs": [{"name", "type",
"comment"}, ...]}`.

> **If you change tool code, restart the registration.** Python
> imports are cached per-process; an MCP subprocess that was spawned
> before your change is still running the old code. Fix:
> `claude mcp remove databricks-steward` then re-add. Or start a
> fresh Claude Code session.

Per-client recipes (Claude Desktop, Cursor, Goose, LangChain,
TypeScript SDK, MCP Inspector) live in
[`COMPATIBILITY.md`](COMPATIBILITY.md).

---

## 6. Call a tool directly (without an MCP client)

For iteration while writing tools, it's faster to bypass the MCP
protocol:

```bash
python -c "
import asyncio
from mcp_server.tools.basic_tools import list_catalogs
print(asyncio.run(list_catalogs()))
"
```

The async version is what `@safe_tool()` registers — synchronous
calls would block the event loop, see
[`STRESS_FINDINGS.md`](STRESS_FINDINGS.md) §A1 / §D for why
`@safe_tool` rejects sync `def` tools by default.

---

## 7. Adding a new tool

1. Create a module under `mcp_server/tools/`, for example
   `schema_tools.py`. Use `@safe_tool()` from `mcp_server.app` —
   it wraps your function with the reliability + audit + rate-limit
   layer:

   ```python
   from mcp_server.app import safe_tool
   from mcp_server.databricks.client import get_workspace, run_in_thread

   @safe_tool()
   async def list_tables(catalog: str, schema: str) -> dict:
       """List tables in a Unity Catalog schema.

       Returns each table's name, type, and comment. Authentication
       comes from DATABRICKS_HOST + DATABRICKS_TOKEN.
       """
       def _call():
           return list(get_workspace().tables.list(
               catalog_name=catalog, schema_name=schema,
           ))
       tables = await run_in_thread(_call)
       return {"tables": [
           {"name": t.name, "type": t.table_type.value if t.table_type else None,
            "comment": t.comment}
           for t in tables
       ]}
   ```

   Type-hint your parameters and return value — FastMCP derives the
   tool's JSON schema from the signature, and that schema is what
   the calling LLM sees.

2. **Import the module in `mcp_server/server.py`** so the decorator
   runs at startup:

   ```python
   from mcp_server.tools import (  # noqa: F401
       basic_tools, health, schema_tools, sql_tools,
   )
   ```

   This is the main footgun: forget the import and the tool
   silently never registers. [`FLOW.md`](FLOW.md) explains why.

3. Use `run_in_thread(...)` for any synchronous SDK call.
   `databricks-sdk` is sync; calling it directly from an async tool
   blocks the event loop and breaks concurrency, cancellation, and
   shutdown handling
   ([`STRESS_FINDINGS.md`](STRESS_FINDINGS.md) §A1).

4. Tests go under `tests/`. Mock the workspace via
   `mcp_server.databricks.client.set_workspace_for_tests(mock)` —
   real workspace probes belong in `stress/`, not in unit tests.

5. Run the full check before committing:

   ```bash
   pytest tests/                 # 107 tests, ~3s
   ruff check mcp_server/ tests/
   mypy mcp_server/
   bandit -c pyproject.toml -r mcp_server/ --severity-level medium
   ```

   CI runs the same set on every push.

---

## 8. What the reliability layer gives you for free

Every `@safe_tool()`-decorated function gets, without writing any
extra code:

- **Exception capture** — uncaught exceptions become structured
  `{"error": {"type", "message"}}` returns instead of killing the
  stdio process.
- **Response size cap** (`MCP_MAX_RESPONSE_BYTES`, default 256 KB) —
  oversized payloads become a `ResponseTooLarge` error.
- **Per-tool timeout** (`MCP_TOOL_TIMEOUT_S`, default 30 s; SQL
  tools override to 60 s) — async tools that exceed the cap are
  cancelled server-side and return a `ToolTimeout` error.
- **Sync-tool rejection** — `@safe_tool` raises `TypeError` on
  `def` tools by default. Pass `allow_sync=True` for fast pure-CPU
  work.
- **Audit log** — `tool.start` + `tool.end` JSONL records sharing a
  `request_id`, with caller-id, latency, outcome, error type,
  response bytes. SQL tools also emit `tool.databricks_statement`
  with the workspace `statement_id` so an operator can pivot to
  `system.query.history`.
- **Hash chain** — every record carries `seq` + `prev_hash` +
  `hash` (SHA-256). Verify with
  `python -m mcp_server.audit_verify <path>`.
- **Per-(tool, caller) rate limit** — token bucket charged on
  entry. Override with `MCP_RATE_LIMIT="execute_sql_safe=10/60"`.
- **Caller identity propagation to Databricks** — every SQL
  statement is tagged `mcp_caller=<id>` and visible in
  `system.query.history`.

All documented in [`SECURITY.md`](SECURITY.md) "Threat-mitigation
matrix" with the test names that verify each guard.

---

## 9. Project layout

```
databricks-ai-steward/
├── mcp_server/
│   ├── app.py                  # FastMCP instance + @safe_tool guards
│   ├── server.py               # Entry point: argparse + transport selection
│   ├── lifecycle.py            # Stdio signal handling, graceful drain
│   ├── audit.py                # JSONL audit log + hash chain
│   ├── audit_verify.py         # CLI: verify chain integrity
│   ├── rate_limit.py           # Per-(tool, caller) token bucket
│   ├── telemetry.py            # Prometheus + OpenTelemetry (opt-in)
│   ├── databricks/             # Workspace client + sql_safety gate
│   └── tools/                  # @safe_tool-decorated functions
├── deploy/
│   ├── helm/databricks-ai-steward/   # Production helm chart
│   └── docker-compose.yml      # Single-host deployment
├── stress/                     # Load + fault-injection probes
├── tests/                      # 107 pytest tests, 81% coverage
├── docs/EXERCISE.html          # Print-friendly exercise guide
├── pyproject.toml
├── Dockerfile
├── requirements.lock
├── README.md
├── CLAUDE.md                   # Operating notes for Claude Code
├── WALKTHROUGH.md              # This file (setup + tool authoring)
├── EXERCISE.md                 # Hands-on feature tour
├── FLOW.md                     # How requests move through the system
├── SECURITY.md                 # Threat model + env-var reference
├── RUNBOOK.md                  # Operator's guide
├── INCIDENT_RESPONSE.md        # Compromise playbooks
├── COMPLIANCE.md               # SOC 2 / ISO 27001 control mapping
├── AUDIT_LOG_SCHEMA.md         # Audit-log schema + verification recipes
├── COMPATIBILITY.md            # Per-client compatibility matrix
├── STRESS_FINDINGS.md          # Empirical issues found by stress probes
├── FAILURE_MODES.md            # Failure-mode catalog
└── AGENTS.md                   # Original goal statement
```
