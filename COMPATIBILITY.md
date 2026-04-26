# databricks-ai-steward — Client Compatibility

Empirical record of which MCP clients have been tested against this
server, on which transport, and what worked. Keep this honest:
"untested" is not a synonym for "incompatible."

---

## Tested

| Client | Transport | Tested on | tools/list | tools/call | Notes |
|---|---|---|---|---|---|
| [MCP Inspector](https://github.com/modelcontextprotocol/inspector) (CLI mode) | stdio | 2026-04-25 | ✓ | ✓ | Automated by `stress/probe_inspector_compat.py` |
| MCP Inspector (CLI mode) | streamable-http | 2026-04-25 | ✓ | ✓ | Same probe |
| MCP Python SDK ClientSession | stdio | 2026-04-25 | ✓ | ✓ | Used by every probe under `stress/` |
| MCP Python SDK ClientSession | streamable-http | 2026-04-25 | ✓ | ✓ | `stress/probe_http_transport.py` |
| MCP TypeScript SDK Client | stdio | 2026-04-26 | ✓ | ✓ | `stress/probe_typescript_compat.py`; cross-language signal |
| MCP TypeScript SDK Client | streamable-http | 2026-04-26 | ✓ | ✓ | Same probe |
| Goose (Block) CLI 1.32.0 | stdio | 2026-04-26 | ✓ | partial | Extension loaded cleanly, session ran with our server registered. Tool invocation via LLM not run end-to-end (would require fresh keychain auth grant + Anthropic credits). See "Goose recipe" below. |
| Claude Desktop (macOS) | stdio | 2026-04-26 | ✓ | ✓ | Verified end-to-end after relaunch. Config in `~/Library/Application Support/Claude/claude_desktop_config.json`. |
| Cursor (macOS) | stdio | 2026-04-26 | ✓ | ✓ | Verified end-to-end: Composer agent called `list_catalogs` via MCP and returned the expected stub. Config in this repo's `.cursor/mcp.json` (gitignored). |

The MCP Inspector pass is the strongest single signal — it is the
reference debugging tool from the MCP team and exercises the spec
end-to-end. If a new client breaks but Inspector still works, the
problem is most likely client-side, not server-side.

The MCP TypeScript SDK pass is the second strongest: it validates that
the spec implementation is correct *across languages*, not just within
the Python SDK ecosystem that drives most other probes. Most
non-Python clients (Claude Desktop, Cursor, Cline, Goose, Continue.dev)
sit on top of `@modelcontextprotocol/sdk` for Node, so this is direct
evidence those clients should also work.

---

## Not yet tested (priority order)

| Client | Transport | Priority |
|---|---|---|
| Cline (VS Code) | stdio | High — popular dev workflow |
| LangChain MCP adapters | stdio | Medium — agent framework wrapper |
| LangGraph | via LangChain | Low — same SDK underneath |
| OpenAI Agents SDK | stdio + http | Low — newer; often paywalled |

---

## How to run the automated checks

```bash
source .venv/bin/activate

# Inspector (stdio + streamable-http)
python -m stress.probe_inspector_compat
# requires Node + npx; Inspector is fetched via npx on first run

# TypeScript SDK (stdio + streamable-http)
python -m stress.probe_typescript_compat
# requires Node + npx; runs `npm install` in stress/ts/ on first run

# Python SDK over HTTP
python -m stress.probe_http_transport
```

Both probes are runnable in CI once Node is available on the runner.

---

## Cursor recipe

Cursor (the IDE) reads MCP server registrations from one of two
locations:
- **Global** (all projects): `~/.cursor/mcp.json`
- **Project** (this repo only): `.cursor/mcp.json` at the repo root

The format is identical to Claude Desktop. Project-level is preferred
for portfolios because the registration travels with the repo (note
the actual file is gitignored because it contains a machine-specific
absolute path; commit a `.cursor/mcp.json.example` if you want to
share):

```json
{
  "mcpServers": {
    "databricks-steward": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "mcp_server.server"]
    }
  }
}
```

After editing, reload Cursor (Cmd+Shift+P → *Developer: Reload Window*,
or fully quit and relaunch). Cursor only spawns MCP server subprocesses
on reload.

**To verify in the UI:**
1. Open Cursor Settings (Cmd+,) → search for "MCP" or click the *MCP*
   tab.
2. `databricks-steward` should appear in the list with a green
   "connected" indicator and the discovered tool count (2: `health`,
   `list_catalogs`).
3. Open Composer or the chat sidebar and ask Cursor's agent: *"use
   the databricks-steward MCP server's list_catalogs tool"*. It
   should call the tool and show the catalog list.

**To remove**: edit `.cursor/mcp.json` to drop the entry, or delete
the file entirely. Reload.

**Troubleshooting**: if the server doesn't connect, the MCP tab in
Cursor settings shows a red status with the spawned subprocess's
stderr. Common causes are the same as Claude Desktop: wrong `command`
path, or a JSON typo.

---

## Claude Desktop recipe

Claude Desktop on macOS reads MCP server registrations from
`~/Library/Application Support/Claude/claude_desktop_config.json`.
Add a stdio entry under `mcpServers`:

```json
{
  "mcpServers": {
    "databricks-steward": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "mcp_server.server"]
    }
  }
}
```

After editing the config, **fully quit Claude Desktop (Cmd+Q — closing
the window is not enough)** and relaunch. Claude Desktop only spawns
MCP server subprocesses at startup.

**To verify in the UI:**
1. In a new chat, click the tools / sliders icon in the message
   composer.
2. `databricks-steward` should appear in the list of connected
   servers, showing `list_catalogs` and `health` as available tools.
3. Ask Claude something like "use list_catalogs from
   databricks-steward". It should call the tool and return
   `{"catalogs": ["main", "analytics", "system"]}`.

**To remove**: edit the config, drop the `databricks-steward` entry,
quit + relaunch.

**Troubleshooting**: if the server doesn't appear, check Claude
Desktop's developer log at
`~/Library/Logs/Claude/mcp-server-databricks-steward.log` for the
spawned subprocess's stderr. Common causes: wrong `command` path
(must be the absolute path to your venv's python), or a typo in the
JSON (Claude Desktop silently skips invalid entries).

---

## Goose recipe

Block's [Goose](https://block.github.io/goose/) (`brew install
block-goose-cli`) loads MCP servers as "extensions" configured in
`~/.config/goose/config.yaml`. To register this server for stdio:

```yaml
extensions:
  databricks-steward-stdio:
    enabled: true
    type: stdio
    name: databricks-steward (stdio)
    cmd: /absolute/path/to/.venv/bin/python
    args: [-m, mcp_server.server]
    envs: {}
    env_keys: []
    timeout: 60
    bundled: null
    available_tools: []
```

For HTTP, use the same shape with `type: streamable_http` and a `uri:`
field pointing at `http://127.0.0.1:8765/mcp` (after starting the
server with `--transport streamable-http`). Add `headers:` with
`Authorization: Bearer ...` if `MCP_BEARER_TOKEN` is set.

**Verification recipe.** A simple session that exercises the tool:

```bash
goose run -t "Use the databricks-steward extension's list_catalogs tool. \
Return only the JSON output."
```

A successful run prints something containing
`{"catalogs": ["main", "analytics", "system"]}`. This requires Goose's
configured LLM provider to be reachable (Anthropic / OpenAI key in
keychain or env).

---

## Adding a new client to the matrix

1. Pick a transport the client supports (stdio is universal; HTTP for
   web-hosted harnesses).
2. Configure the client to spawn / connect to the server. For stdio,
   the launch command is typically `python -m mcp_server.server`. For
   HTTP, point at `http://HOST:PORT/mcp` (streamable-http) or
   `http://HOST:PORT/sse` (SSE).
3. Verify three things at minimum:
   - server appears in the client's tool list with `health` and
     `list_catalogs` visible
   - `list_catalogs` returns the stub payload
   - `health` reports `ready=true`
4. Add a row to the **Tested** table above with the date and any quirks.
5. If the client adds new compat surface (e.g. cancellation, sampling,
   resources), note it as a column or a follow-up probe.

---

## Known portability constraints

These are deliberate choices that may matter for some clients:

- **Tools are async-only by default.** `safe_tool` rejects `def` tools
  at registration unless `allow_sync=True`. This is unrelated to client
  compatibility but means tool authors must use `async def`.
- **Per-tool 30s timeout** (`MCP_TOOL_TIMEOUT_S`). A client expecting
  longer-running tools will see `ToolTimeout` errors; tune the env var
  up if needed.
- **256 KB response cap** (`MCP_MAX_RESPONSE_BYTES`). Clients with
  larger context windows may want this raised; clients with smaller
  may want it lowered.
- **stdio uses our custom signal handler.** SIGTERM and SIGINT both
  trigger graceful shutdown; clients that send other signals (SIGHUP)
  fall through to Python defaults.
- **HTTP transports use uvicorn's built-in graceful drain.** Default
  uvicorn timeout (`UVICORN_GRACEFUL_TIMEOUT`) applies; we don't override.
- **HTTP defaults to loopback only.** `MCP_ALLOW_EXTERNAL=1` (or
  `--allow-external`) is required to bind any non-loopback host.
- **HTTP has no built-in auth except optional bearer token.** Set
  `MCP_BEARER_TOKEN` to require `Authorization: Bearer <token>` on
  every request. Pair with TLS for any external exposure. Clients
  consuming the HTTP transport must support custom auth headers.
