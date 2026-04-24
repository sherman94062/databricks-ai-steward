# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project goal

A Databricks AI Steward: an MCP server exposing a governed set of tools that let AI agents interact with Databricks safely. Planned tool surface: `list_catalogs`, `list_tables`, `describe_table`, `sample_table`, `execute_sql_safe`. Cross-cutting concerns: SQL safety, schema discovery, query governance, audit logging.

Current state is scaffolding only — `list_catalogs` is a hardcoded stub, and the `databricks/`, `governance/`, `agents/`, `examples/`, and `tests/` directories are empty placeholders. `README.md` and `PROJECT_SPEC.md` are empty. `AGENTS.md` (from the project's initial Codex bootstrap) restates the goal; prefer this file going forward.

## Commands

Dependencies are declared in `pyproject.toml`. The venv at `.venv/` uses Python 3.14.

```bash
source .venv/bin/activate

# Sync deps after pulling or editing pyproject.toml
pip install -e '.[dev]'   # omit [dev] for runtime-only

# Run the MCP server (stdio transport — blocks waiting for a client)
python -m mcp_server.server

# Register with Claude Code
claude mcp add databricks-steward -- python -m mcp_server.server

# Run tests
pytest tests/
```

## Architecture

### MCP via FastMCP (stdio)

`mcp_server/server.py` runs a `FastMCP` server that speaks the Model Context Protocol over stdio — the transport Claude Code expects. The `FastMCP` instance lives in `mcp_server/app.py` so tool modules can import it without a circular dependency on `server.py`.

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
