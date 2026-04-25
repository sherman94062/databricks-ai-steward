# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project goal

A Databricks AI Steward: an MCP server exposing a governed set of tools that let AI agents interact with Databricks safely. Planned tool surface: `list_catalogs`, `list_tables`, `describe_table`, `sample_table`, `execute_sql_safe`. Cross-cutting concerns: SQL safety, schema discovery, query governance, audit logging.

Current state is scaffolding plus a substantial reliability/transport layer. `list_catalogs` is a hardcoded stub; the `databricks/`, `governance/`, `agents/`, and `examples/` directories are empty placeholders. `PROJECT_SPEC.md` is empty. `README.md`, `WALKTHROUGH.md`, `FLOW.md`, `STRESS_FINDINGS.md`, and `FAILURE_MODES.md` are populated. `AGENTS.md` (from the project's initial Codex bootstrap) restates the goal; prefer this file going forward.

## Commands

Dependencies are declared in `pyproject.toml`. The venv at `.venv/` uses Python 3.14.

```bash
source .venv/bin/activate

# Sync deps after pulling or editing pyproject.toml
pip install -e '.[dev]'   # omit [dev] for runtime-only

# Run the MCP server (defaults to stdio; --transport streamable-http or sse for HTTP)
python -m mcp_server.server
python -m mcp_server.server --transport streamable-http --port 8765

# Register with Claude Code
claude mcp add databricks-steward -- python -m mcp_server.server

# Run tests
pytest tests/
```

## Architecture

### MCP via FastMCP

`mcp_server/server.py` is the entry point. It selects a transport (`stdio` by default; `streamable-http` and `sse` available via `--transport` or `MCP_TRANSPORT`) and delegates to FastMCP. The `FastMCP` instance lives in `mcp_server/app.py` so tool modules can import it without a circular dependency.

For stdio, `mcp_server/lifecycle.py` wraps the run with custom signal handling: SIGTERM/SIGINT close stdin (forcing the anyio reader thread to release — asyncio cancellation alone cannot interrupt it), the server task exits, registered cleanup callbacks run, and the process exits with code 0. For HTTP transports, uvicorn handles graceful shutdown natively.

### Adding a tool

1. Write a decorated function in a module under `mcp_server/tools/`, using `@safe_tool()` from `mcp_server.app`:

   ```python
   from mcp_server.app import safe_tool

   @safe_tool()
   def my_tool(arg: str) -> dict:
       """Docstring — FastMCP uses this as the tool description."""
       ...
   ```

   `safe_tool` wraps `@mcp.tool()` with shared exception-catching and response-size guards (see next section). Prefer it over raw `@mcp.tool()` unless you have a specific reason to bypass the guards.

2. Import the module in `mcp_server/server.py` so the decorator runs at startup. Forgetting this import is the main footgun: the tool won't register, and there will be no error. All tool-module imports in `server.py` should be marked `# noqa: F401` since they're side-effect imports.

FastMCP derives each tool's JSON schema from the function's type hints and docstring, so type your parameters and return values accurately.

### Reliability guards

`mcp_server/app.py` applies three process-level protections because a stdio MCP server dies hard on uncaught errors:

- **Logs go to stderr.** Writing to stdout corrupts the JSON-RPC stream; `logging.basicConfig` pins the root logger to stderr. Never use `print()` in tool code — it will silently break the session. Level tunable via `MCP_LOG_LEVEL` env var.
- **Exceptions in tool code become structured error responses.** `@safe_tool()` catches anything a tool raises and returns `{"error": {"type": ..., "message": ...}}` rather than letting the exception escape to the event loop.
- **Oversized responses are rejected.** Serialized tool returns larger than `MAX_RESPONSE_BYTES` (default 256 KB, override via `MCP_MAX_RESPONSE_BYTES`) are replaced with a `ResponseTooLarge` error. This protects both the server and the client's context window.

Tests in `tests/test_guards.py` cover each guard; run them with `pytest tests/`.
