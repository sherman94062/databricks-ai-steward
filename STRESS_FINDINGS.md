# databricks-ai-steward — Stress Findings

Empirical issues surfaced by the load and cancellation harnesses under
`stress/`. Each finding is reproducible from a single command. Fixes
that have landed are noted; the rest are open.

---

## Summary

| ID | Finding | Severity | Status |
|---|---|---|---|
| A1 | Sync tool blocks the event loop, wedging the entire session | High | **Fixed** — `safe_tool` now refuses sync tools |
| A.1 | Client cancellation does not propagate; cancelled async calls leak server-side coroutines | High | **Mitigated** — per-tool server-side timeout; upstream bug remains |
| B2 | Sync hang prevents clean shutdown on stdin EOF (requires SIGKILL) | Medium | Inherits A1 fix |
| C1 | SIGINT is swallowed during in-flight calls (SIGTERM works) | Medium | Open — clients should signal with SIGTERM |
| D | A single sync slow call kills 100% of concurrent fast calls; async slow adds ~3 ms | High | Inherits A1 fix |
| E2 | `_cap_response`'s json.dumps validation diverges from FastMCP's wire encoding for deeply nested objects | Low | Open — guard's promise leaks |

---

## Reproduction

All probes assume the venv is active and require no Databricks credentials.

```bash
source .venv/bin/activate

# Throughput / load baseline (no issues found at this layer)
python -m stress.load --concurrent 100 --total 5000

# Cancellation / lifecycle probes
python -m stress.probe_a_correlation     # A1
python -m stress.probe_a1_leak           # A.1
python -m stress.probe_a1_fix_verify     # A.1 fix verification
python -m stress.probe_b_disconnect      # B2
python -m stress.probe_c_signal          # C1
python -m stress.probe_d_blast_radius    # D

# Boundary conditions
python -m stress.probe_e_boundary        # E1–E5
```

---

## A1 — Sync tools wedge the session

**Symptom.** A `def`-defined tool that blocks (e.g. `time.sleep`,
`requests.get`, sync DB driver) makes the server unresponsive to *every*
subsequent call on the session — not just the slow one. The session is
unrecoverable; only teardown clears it.

**Probe output.**

```
[trial] hang_tool=hangs_forever_guarded
  → WEDGED — follow-up timed out after 2.00s
[trial] hang_tool=hangs_forever_async_guarded
  → recovered in 3.5ms — {"ok": true}
```

**Root cause.** FastMCP dispatches sync tools on (or via a single
serialized worker behind) the asyncio event loop. While the sync tool is
running, the loop cannot read further requests from stdin. With a real
sync DB driver, a 30-second query blocks every other request for the
full 30 seconds.

**Fix in this repo.** `safe_tool` now raises `TypeError` at registration
time if a tool is `def` rather than `async def`. Tools must be `async
def` and wrap blocking I/O in `asyncio.to_thread(...)`. Pass
`allow_sync=True` only for fast pure-CPU work.

```python
@safe_tool()                       # OK if my_tool is async def
async def my_tool() -> dict: ...

@safe_tool(allow_sync=True)        # OK — explicit opt-in
def trivial(x: int) -> int: ...
```

---

## A.1 — Async cancellation leaks server-side coroutines

**Symptom.** When a client cancels `await session.call_tool(...)` (e.g.
via `asyncio.wait_for` timeout), the server-side coroutine is **not**
cancelled. Each cancelled call leaves a suspended task on the server
holding whatever resources its coroutine held (DB connections, cursors,
locks).

**Probe output.**

```
[baseline] task_count = 4
[after 50 cancelled hangs] task_count = 54
[delta] 50 additional tasks
[after 0.5s settle] task_count = 54
```

**Root cause (upstream bug).** The MCP Python SDK
(`mcp/shared/session.py:send_request`) does not send a
`notifications/cancelled` JSON-RPC notification when its `call_tool` task
is cancelled. The server-side handler in the same SDK exists and would
correctly cancel the in-flight task — it just never receives the
notification.

```python
# mcp/shared/session.py — what's missing in the client
except anyio.get_cancelled_exc_class():
    await self.send_notification(types.ClientNotification(
        types.CancelledNotification(
            params=types.CancelledNotificationParams(requestId=request_id)
        )
    ))
    raise
```

The server already handles the notification correctly:

```python
# mcp/shared/session.py:402  — server-side handler is in place
if isinstance(notification.root, CancelledNotification):
    cancelled_id = notification.root.params.requestId
    if cancelled_id in self._in_flight:
        await self._in_flight[cancelled_id].cancel()
```

**Mitigation in this repo.** `safe_tool` applies a server-side per-tool
timeout (`MCP_TOOL_TIMEOUT_S`, default 30s) via `asyncio.wait_for`. A
tool that exceeds the timeout is cancelled by the server and returns a
`ToolTimeout` structured error. This bounds the leak window even when
the client never sends a cancellation notification.

**Verified.** `probe_a1_fix_verify` shows `task_count` returning to
baseline within 1s after 50 client-cancelled calls when the underlying
tool has `timeout_s=0.5`.

**Still open.** The upstream SDK bug is unfixed. A patch on the client
SDK would let cancellations propagate immediately, sub-second, without
needing a server-side timeout. Worth filing.

---

## B2 — Sync hang blocks clean shutdown

**Symptom.** Closing the client stdin during an in-flight tool call:

| Tool kind | Exit code | Time after stdin close | Needed SIGKILL |
|---|---|---|---|
| `async def` | 0 | 0.21 s | No |
| `def` | -9 | 5.18 s | **Yes** |

**Root cause.** Same as A1 — sync tools own the event loop, so when
stdin closes the loop cannot reach its EOF-detection / shutdown logic.

**Fix.** Inherits A1's `safe_tool` rejection of sync tools.

---

## C1 — SIGINT is not honored mid-call

**Symptom.** Sending a signal to the server subprocess during a hanging
tool call:

| Tool kind | Signal | Exit code | Time | Needed SIGKILL |
|---|---|---|---|---|
| async | SIGINT | -9 | 5.16 s | **Yes** |
| sync | SIGINT | -9 | 5.18 s | **Yes** |
| async | SIGTERM | -15 | 0.10 s | No |
| sync | SIGTERM | -15 | 0.11 s | No |

**Root cause.** Either FastMCP or the MCP SDK installs a SIGINT handler
intended for graceful cleanup; that cleanup awaits the in-flight tool,
which never returns. SIGTERM is not caught — Python's default handler
terminates the process immediately.

**Open.** No clean fix at the application layer. Operators should signal
shutdown with SIGTERM, not SIGINT. Worth confirming what Claude Code
sends to its MCP subprocesses.

---

## D — Blast radius of a single slow call

**Symptom.** Fifty fast (`ok_guarded`) calls in flight while one slow
tool runs:

| Scenario | Success | p50 latency |
|---|---|---|
| Baseline (no slow call) | 50 / 50 | 33.6 ms |
| Async slow in flight | 50 / 50 | 36.7 ms |
| Sync slow in flight | **0 / 50** | — |

A single async slow call adds ~3 ms (statistical noise) to concurrent
fast calls. A single sync slow call kills 100% of them.

**Root cause.** Same as A1.

**Fix.** Inherits A1's rejection of sync tools.

---

## E2 — Guard's serializability check diverges from wire encoding

**Symptom.** A return value with deeply nested structure (depth ≥ 500)
passes `_cap_response`'s `json.dumps` check, so the guard returns the
original object — but FastMCP's wire encoder then fails on the same
object. The client receives a raw `isError=True` from FastMCP instead of
the structured `ResponseNotSerializable` the guard promises.

**Probe output.**

```
E2a nested depth=500    isError=True | Error executing tool ...: Error serializing to JSON: ...
E2b nested depth=2000   isError=True | ...
E3 circular reference   {"error": {"type": "ResponseNotSerializable", ...}}   ← guard works here
```

**Root cause.** `_cap_response` does double work: it serializes once for
size validation, then returns the *original* object for FastMCP to
serialize again on the wire. The two encoders have different limits and
different `default=` fallbacks, so payloads exist that pass our check
and fail downstream.

**Open.** Two ways to close the gap: (1) cache the serialized form and
pass it through; (2) match FastMCP's encoder configuration when
validating. Severity is low — the server doesn't crash, the client just
gets a less informative error.

---

## What the load harness did *not* find

The load harness in `stress/load.py` ran **126,000 calls across c=1 to
c=20,000 concurrent** with zero errors. Throughput plateaus at ~2,400
calls/s (the stdio JSON-RPC pipe is the bottleneck) and degrades
linearly past saturation — graceful queueing, no thrashing.

This means the protocol layer + reliability guards hold under load on
the happy path. All real findings come from the cancellation /
lifecycle harness, where state spans more than one request.

---

## Recommended next probes

Not yet run; ranked by likelihood of finding more issues:

1. **Soak test.** Run `stress.load` at c=10 for 30+ minutes, sample RSS
   every 10 s. Even a 1 MB / minute leak is a finding.
2. **Mixed-workload concurrency.** Interleave size-rejected, raising,
   and normal tools at c=50. Tests whether one tool's failure path can
   corrupt another's response.
3. **Subprocess churn.** Spawn / init / teardown 200 sessions in a tight
   loop — exercises shutdown ordering, fd cleanup, atexit handlers.
4. **Adversarial tool args.** Very long strings, unicode edge cases
   (surrogates, NUL bytes, RTL marks), wrong types. FastMCP's pydantic
   validation should catch most, but the error path under load is rarely
   tested.
