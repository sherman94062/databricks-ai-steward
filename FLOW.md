# databricks-ai-steward — Flow

How a request moves through the system, from Claude Code down to the tool function and back. Useful for understanding *why* the code is organized the way it is, and where future layers (governance, audit, Databricks client) will plug in.

---

## The big picture

```
┌──────────────────────────────────────────────────────────────┐
│ Claude Code (MCP client)                                     │
│   The model decides to call a tool based on user intent.     │
└──────────────────────────────────────────────────────────────┘
                │  JSON-RPC over stdio
                │  { "method": "tools/call",
                │    "params": { "name": "list_catalogs", ... } }
                ▼
┌──────────────────────────────────────────────────────────────┐
│ mcp_server/server.py  (entry point)                          │
│   1. load_dotenv() — reads .env into os.environ             │
│   2. imports mcp_server.app   → constructs FastMCP instance │
│   3. imports mcp_server.tools.basic_tools                   │
│        └─ side effect: @mcp.tool() decorators run,          │
│           registering functions on the FastMCP instance     │
│   4. mcp.run() — enters the stdio serve loop                │
└──────────────────────────────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────────────┐
│ FastMCP dispatch                                             │
│   • Parses the JSON-RPC message                              │
│   • Validates arguments against the tool's JSON schema       │
│     (derived from the Python function's type hints)          │
│   • Looks up the registered callable by name                 │
└──────────────────────────────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────────────┐
│ Tool function — e.g. mcp_server/tools/basic_tools.py         │
│   def list_catalogs() -> dict: ...                           │
│                                                              │
│   [PLANNED: governance pre-check]                            │
│   [PLANNED: Databricks client call]                          │
│   [PLANNED: audit log write]                                 │
│   return {"catalogs": [...]}                                 │
└──────────────────────────────────────────────────────────────┘
                │  return value
                ▼
┌──────────────────────────────────────────────────────────────┐
│ FastMCP serializes the return → JSON → stdout → Claude Code │
└──────────────────────────────────────────────────────────────┘
```

---

## Why the code is split across three files

`FastMCP` requires a single `mcp` object that holds all registered tools. Three concerns fight over where it should live:

1. **Tool modules** need to import it (to call `@mcp.tool()`).
2. **The entry point** needs to import it (to call `mcp.run()`).
3. **Tool modules must be imported before the server runs**, or their decorators never execute and the tools silently disappear.

If the `mcp` instance lived in `server.py`, tool modules would import from `server.py`, and `server.py` would also import tool modules — a circular import that Python would either reject or partially resolve in surprising ways.

The fix: a third file, `mcp_server/app.py`, whose *only* job is to construct the `FastMCP` instance. Both tool modules and `server.py` import from it; neither depends on the other.

```
              mcp_server/app.py           (defines mcp)
                /          \
               ▼            ▼
  tools/basic_tools.py    server.py
  (registers tools)       (imports tools for side effects,
                           then calls mcp.run())
```

---

## The "silent failure" gotcha, explained

The `@mcp.tool()` decorator only runs when its containing module is imported. If `server.py` never imports `mcp_server.tools.my_new_tool`, then Python never executes the decorator, the function never lands in the registry, and the MCP protocol reports no such tool — with no warning anywhere.

This is why `server.py` contains seemingly-unused imports marked `# noqa: F401`. They exist *to cause side effects*. Every new tool module must be added to that import list.

(There are auto-discovery patterns — `pkgutil.walk_packages` over `mcp_server.tools`, for example — that would eliminate this gotcha. They're worth considering once the tool count grows, but add magic that makes the import graph harder to reason about when debugging. For now: explicit imports.)

---

## Where future layers plug in

### Databricks client (`databricks/`)

The tool function is the right layer to call into the Databricks client — *not* the FastMCP dispatcher, not a middleware. Keep tools thin: each tool should assemble arguments, call one method on a `DatabricksClient` wrapper, and shape the response.

```
tool function
   └─ DatabricksClient.list_catalogs()   ← thin wrapper over databricks-sdk
        └─ WorkspaceClient(...).catalogs.list()
```

This keeps `databricks-sdk` as a leaf dependency: if the SDK changes, only the wrapper moves.

### Governance (`governance/`)

Governance sits **between** the tool entry and the Databricks call:

```
tool function
   ├─ governance.check(request)   ← may raise PolicyViolation
   ├─ DatabricksClient.execute(...)
   └─ audit.record(request, result)
```

For `execute_sql_safe` specifically, the pre-check parses the SQL (candidate: `sqlglot`), asserts SELECT-only, enforces row caps, and flags PII columns. A policy violation should return a structured error to the caller, not a 500.

### Audit log

Audit should run on *every* tool call, including failures. The cleanest place is a decorator or context manager that wraps the tool body, so an individual tool can't forget to log. If that pattern is adopted, add it in `app.py` next to the `FastMCP` construction so it's applied uniformly:

```python
# pseudocode
@mcp.tool()
@audited
def list_catalogs() -> dict: ...
```

---

## Transport choice: why stdio

Claude Code speaks MCP over stdio — it spawns the server as a subprocess and exchanges JSON-RPC on stdin/stdout. `FastMCP.run()` defaults to stdio for exactly this reason.

The `mcp` SDK also supports HTTP/SSE transport if the server ever needs to be reached over the network (e.g. a shared instance serving multiple clients). That's a configuration change in `mcp.run()`, not a rewrite. For single-user local development, stdio is simpler: no port, no auth, no TLS.
