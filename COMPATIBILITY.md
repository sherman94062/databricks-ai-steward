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

The MCP Inspector pass is the strongest single signal — it is the
reference debugging tool from the MCP team and exercises the spec
end-to-end. If a new client breaks but Inspector still works, the
problem is most likely client-side, not server-side.

---

## Not yet tested (priority order)

| Client | Transport | Priority |
|---|---|---|
| Claude Desktop | stdio | High — most common consumer-facing client |
| Cursor | stdio | High — IDE adoption is broad |
| Cline (VS Code) | stdio | High — popular dev workflow |
| Goose (Block) | stdio + http | Medium — both transports in one tool |
| MCP TypeScript SDK | stdio + http | Medium — cross-language compliance signal |
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

# Python SDK over HTTP
python -m stress.probe_http_transport
```

Both probes are runnable in CI once Node is available on the runner.

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
