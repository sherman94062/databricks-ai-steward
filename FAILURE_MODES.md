# MCP Server Failure Modes — Observed

Empirical results from running `stress.harness` against a deliberately-misbehaving FastMCP server (`stress.server`). Each scenario spawns a fresh server subprocess, issues one tool call with the MCP Python SDK as client, and records the outcome.

Written 2026-04-24 against `mcp==1.27.0`, Python 3.14. Behavior of FastMCP has shifted over versions — re-run the harness before trusting any of this at a new version.

---

## The one-line summary

**Raw `@mcp.tool()` in FastMCP 1.27 is already defensive against the most common footguns** — tool exceptions, JSON-serialization failures, and (surprisingly) stray `print()` calls all fail safely on their own. Most of what `@safe_tool` adds here is a **response size cap** and a **uniform error shape**. The real remaining gaps are **timeouts** and **resource bounds**, which no FastMCP default protects against.

---

## Results

| Scenario | Expected | Observed | Survived? |
|---|---|---|---|
| baseline (guarded) | success | `{"ok": true}` | ✅ |
| raises (unguarded) | server crash / protocol fail | `isError=true` + message; clean | ✅ |
| raises (guarded) | structured `{"error": {...}}` | structured error | ✅ |
| oversize return (unguarded) | either delivered or protocol fail | **1 MB payload delivered successfully** | ✅ |
| oversize return (guarded) | `ResponseTooLarge` | `ResponseTooLarge` structured error | ✅ |
| **stdout pollution** | **session death from `print()`** | **session survived; tool returned OK** | ✅ 🎉 |
| hang (no timeout) | client timeout; server still spinning | client timed out at 2s; subprocess alive | ⚠️ |
| unserializable (unguarded) | crash / silent failure | `isError=true` + "Error serializing to JSON" | ✅ |
| unserializable (guarded) | `ResponseNotSerializable` | structured error | ✅ |

---

## The surprises

### 1. FastMCP catches tool exceptions by default

Raw `@mcp.tool()` with a body that raises `ValueError` returned an MCP `CallToolResult` with `isError=True` and the exception message as text content. No server crash. This is FastMCP's default behavior, not something our guards added.

**What our guard adds on top**: a uniform `{"error": {"type": ..., "message": ...}}` dict in the return value rather than FastMCP's `isError=True` + free-text. Callers can reliably `result.get("error", {}).get("type")` instead of parsing text. Nice to have, not load-bearing.

### 2. FastMCP catches JSON-serialization failures too

An object whose `__repr__` raises inside `json.dumps` — the exact case my test caught as a bug — is also caught by raw FastMCP. Returns `isError=True | Error serializing to JSON: RuntimeError: exploding __repr__`.

**What our guard adds**: a `ResponseNotSerializable` structured error type. Again, uniform shape, but the default also doesn't crash.

### 3. `print()` to stdout does NOT kill the session (in FastMCP 1.27)

This is the big one. The widespread advice — *"never print in an MCP server, it will corrupt the JSON-RPC stream"* — appears to not apply to modern FastMCP. Our `stdout_pollution_guarded` tool calls `print("THIS LINE BREAKS THE MCP PROTOCOL", flush=True)` and then returns normally. The client received the normal return value and the session kept working.

FastMCP likely redirects `sys.stdout` during tool execution (or the SDK's stdio reader tolerates non-JSON bytes). **Do not assume this protection exists in your SDK version** — confirm empirically. The harness is the right way to check: drop a `print()` into a tool and run a call; if it corrupts the stream, you'll see `session_died` in the status column.

### 4. Oversize returns go through untouched

Returning a 1 MB response with no guard completes in ~300 ms and is delivered to the client intact. FastMCP doesn't impose a size cap. This matters because:

- It will burn **Claude's context window** (a 1 MB tool result is ~250K tokens — larger than the entire 200K default context for many models).
- A malicious or buggy tool can deliver arbitrarily large payloads without the server noticing.

**This is the single most valuable thing our `@safe_tool` adds** — `MAX_RESPONSE_BYTES` enforcement.

### 5. Hangs survive the client

`time.sleep(300)` inside a tool is invisible to FastMCP. The client times out at whatever bound it imposes; the server subprocess keeps running the sleep. In practice `stdio_client` kills the subprocess on context exit, so in our harness the process does get reaped. But in a long-lived session (Claude Code stays connected), a hung tool call ties up the event loop until the client gives up — and if the client retries, you pile up concurrent hangs.

**No default FastMCP protection; no protection in our guards either.** This is the most important gap to close when Databricks tools land.

---

## Takeaways — portable to Zepz

A checklist for any new MCP server, ordered by what I'd actually prioritize:

1. **Per-tool timeouts.** Wrap every tool body with `asyncio.wait_for` (or equivalent sync pattern with a watchdog thread). Pick a timeout per tool based on realistic worst-case. Without this, a single slow dependency hangs the whole server.

2. **Response size cap.** FastMCP has none. Reject any serialized return above a threshold (e.g. 256 KB). Protects both the server and the LLM's context window. See `_cap_response` in this repo's `mcp_server/app.py` for a working pattern.

3. **Structured error returns.** FastMCP's `isError=True` + text is usable but not introspectable. If your tools will be consumed by code (agents, pipelines) rather than just an LLM, prefer a uniform `{"error": {"type": ..., "message": ...}}` shape so callers can branch on error type.

4. **Resource bounds on inputs too.** Not tested here, but worth adding: max arg string length, max list-arg element count. A tool like `execute_sql_safe` needs SQL length limits or a malicious client could DoS the parser.

5. **Concurrency isolation.** Not tested in this harness. FastMCP processes calls on an asyncio event loop; async tools can interleave. Any mutable module-level state (connection pools, caches, registries) needs locking or per-call scoping. When we add a real Databricks client, its client object should be per-call or explicitly threadsafe.

6. **Empirical verification of your SDK version.** The most surprising finding here was that `print()` didn't break the protocol in FastMCP 1.27. That could change in 1.28. Run a version-specific stress harness (like this one) as part of your dependency-upgrade checklist.

7. **Don't rely on the transport to protect you.** Every protection above lives in *your* code, not FastMCP's. The transport's job is to move bytes; making sure the bytes are safe to send is yours.

---

## What's NOT covered here

Scenarios I deliberately skipped that are worth exploring if you want a more complete picture:

- **Concurrent calls** during a single session — does FastMCP serialize or interleave? (Harness runs one call per subprocess currently.)
- **Malformed JSON from a malicious client** — bypass the SDK, write arbitrary bytes on stdin. Does the server die or recover?
- **Infinite CPU loops** (`while True: pass`) rather than sleeping hangs — same category as hangs but harder to cancel because no `await` point for the event loop to use.
- **Memory exhaustion** — a tool that allocates until OOM. What does FastMCP do when the subprocess dies mid-call?
- **File descriptor leaks** — a tool that opens connections without closing. Only matters for long-lived sessions.

Each is a small extension to `stress/server.py` + `stress/harness.py` if you want to look.

---

## Running the harness

```bash
python -m stress.harness
```

Outputs a summary table to stderr and a machine-readable JSON blob to stdout. Each scenario runs in its own subprocess, so crashes don't cascade.

Add a new scenario by defining a tool in `stress/server.py` and appending a `Scenario(...)` entry in `stress/harness.py`. Total overhead per scenario: ~300 ms (mostly subprocess spawn + MCP handshake).
