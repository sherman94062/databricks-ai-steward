# databricks-ai-steward — Walkthrough

A step-by-step guide to setting up, running, and extending the Databricks AI Steward MCP server.

> **Current state:** scaffolding. The server runs and one stub tool (`list_catalogs`) returns hardcoded values. The Databricks client, SQL safety layer, governance, and audit logging are planned but not yet implemented.

---

## 1. What this project is

An MCP server that gives AI agents a **governed** interface to Databricks. The intent is that Claude Code (or any MCP client) cannot touch Databricks except through this server, and every tool the server exposes enforces safety rules, logs the call, and returns structured results.

Planned tool surface:

| Tool | Purpose |
|---|---|
| `list_catalogs` | Enumerate Unity Catalog catalogs the caller can see |
| `list_tables` | Enumerate tables in a catalog / schema |
| `describe_table` | Return column definitions and metadata |
| `sample_table` | Return a bounded row sample |
| `execute_sql_safe` | Run a SQL statement with governance checks (SELECT-only, row caps, PII guards, etc.) |

Cross-cutting concerns, all **not yet implemented**: SQL safety validation, schema discovery, query governance policies, audit logging.

---

## 2. Prerequisites

- Python 3.12+ (repo is developed on 3.14)
- A Python venv — one exists at `.venv/`
- Claude Code (or any MCP-compatible client) to exercise the server end-to-end

Planned-but-not-yet-required:

- Databricks workspace URL + personal access token (for when the real client lands)

---

## 3. Install

```bash
cd /Users/arthursherman/databricks-ai-steward
source .venv/bin/activate
pip install -e .
```

Dependencies come from `pyproject.toml` (`mcp`, `python-dotenv`). `pip install -e .` gives you an editable install so code changes are picked up without reinstalling.

---

## 4. Run the server standalone

```bash
python -m mcp_server.server
```

This starts `FastMCP` over stdio. It will block waiting for MCP protocol messages on stdin — this is expected. Kill with Ctrl-C.

To sanity-check that tools are registered without sitting through a full MCP handshake:

```bash
python -c "
from mcp_server.app import mcp
from mcp_server.tools import basic_tools
import asyncio
print([t.name for t in asyncio.run(mcp.list_tools())])
"
# → ['list_catalogs']
```

---

## 5. Register with Claude Code

```bash
claude mcp add databricks-steward -- python -m mcp_server.server
```

After this, launch Claude Code from the project directory. `list_catalogs` will appear as an available tool. Prompt Claude with something like *"list the databricks catalogs"* and it will invoke the tool; the stub will respond with `{"catalogs": ["main", "analytics", "system"]}`.

---

## 6. Call the tool directly (without Claude)

For iteration while writing tools, it's often faster to bypass the MCP protocol entirely:

```bash
python -c "
from mcp_server.tools.basic_tools import list_catalogs
print(list_catalogs())
"
# → {'catalogs': ['main', 'analytics', 'system']}
```

Tools are plain Python functions — the `@mcp.tool()` decorator registers them but does not wrap their behavior.

---

## 7. Adding a new tool

1. Create a module under `mcp_server/tools/`, for example `schema_tools.py`:

   ```python
   from mcp_server.app import mcp

   @mcp.tool()
   def list_tables(catalog: str, schema: str) -> dict:
       """List tables in a Unity Catalog schema."""
       # TODO: real Databricks client
       return {"tables": []}
   ```

2. **Import the module in `mcp_server/server.py`** so the decorator runs at startup:

   ```python
   from mcp_server.tools import basic_tools, schema_tools  # noqa: F401
   ```

   This is the main footgun: forget the import and the tool silently never registers — no error, just missing from `list_tools()`. FLOW.md explains why.

3. Type your parameters and return values. FastMCP derives the tool's JSON schema directly from the function signature, so `catalog: str` becomes a required string parameter in the MCP schema, visible to the calling model.

---

## 8. Extending toward the real design

These are the next meaningful pieces of work, roughly in order:

1. **Databricks client** — populate `databricks/`. A thin wrapper around the Databricks SDK (`databricks-sdk`) that reads workspace URL + token from env vars loaded by `load_dotenv()`. Tools call into this wrapper; they should not talk to `databricks-sdk` directly.
2. **SQL safety** — populate `governance/`. Parse SQL (e.g. via `sqlglot`), reject non-SELECT statements by default, cap row counts, strip/flag statements touching PII columns. This layer wraps `execute_sql_safe`.
3. **Audit logging** — a structured log of every tool call (tool name, arguments, caller, decision, result size). Can live alongside governance; at minimum should write JSONL somewhere durable.
4. **Tests** — `tests/` is empty. `pytest` is not installed; add it as a dev dependency in `pyproject.toml` under `[project.optional-dependencies]`.

---

## 9. Project layout

```
databricks-ai-steward/
├── mcp_server/
│   ├── app.py              # FastMCP instance (singleton)
│   ├── server.py           # Entry point; imports tool modules, calls mcp.run()
│   └── tools/
│       └── basic_tools.py  # list_catalogs (stub)
├── databricks/             # Empty — planned Databricks client wrapper
├── governance/             # Empty — planned SQL safety / policy layer
├── agents/                 # Empty — planned agent examples
├── examples/               # Empty — planned client usage examples
├── tests/                  # Empty
├── pyproject.toml
├── CLAUDE.md               # Context for Claude Code
├── FLOW.md                 # How requests move through the system
├── WALKTHROUGH.md          # This file
└── AGENTS.md               # Original goal statement (pre-Claude Code)
```
