"""A deliberately-misbehaving MCP server for fault injection testing.

Pairs each pathological behavior with two variants:
  * _unguarded — registered with plain @mcp.tool() (no protection)
  * _guarded   — registered with our production _guard wrapper

Running as an MCP server over stdio; driven by stress.harness. Not imported
by the production server.
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from mcp_server.app import _guard  # reusing the production guard logic
from mcp_server.lifecycle import lifespan

# Reuse the production lifespan so cleanup callbacks fire on both stdio
# and HTTP shutdown paths (when probes run with --transport flag).
mcp = FastMCP("stress-test", lifespan=lifespan)


# ---- baseline ----------------------------------------------------------
@mcp.tool()
@_guard
def ok_guarded() -> dict:
    """Well-behaved tool; sanity baseline."""
    return {"ok": True}


# ---- raises ------------------------------------------------------------
@mcp.tool()
def raises_unguarded() -> dict:
    """Raises ValueError without protection."""
    raise ValueError("intentional failure from raises_unguarded")


@mcp.tool()
@_guard
def raises_guarded() -> dict:
    """Raises ValueError inside the guard wrapper."""
    raise ValueError("intentional failure from raises_guarded")


# ---- oversize response -------------------------------------------------
@mcp.tool()
def oversize_unguarded() -> dict:
    """Returns ~1 MB payload with no size cap."""
    return {"data": "x" * 1_000_000}


@mcp.tool()
@_guard
def oversize_guarded() -> dict:
    """Returns ~1 MB payload; guard should reject."""
    return {"data": "x" * 1_000_000}


# ---- stdout pollution --------------------------------------------------
@mcp.tool()
@_guard
def stdout_pollution_guarded() -> dict:
    """Writes to stdout before returning.

    The guard wraps return values, not side effects; this write hits the
    MCP transport directly and corrupts the JSON-RPC stream.
    """
    print("THIS LINE BREAKS THE MCP PROTOCOL", flush=True)
    return {"ok": True}


# ---- hang --------------------------------------------------------------
@mcp.tool()
@_guard
def hangs_forever_guarded() -> dict:
    """Sleeps far longer than any client should wait, using a *blocking* sleep.
    This blocks the entire event loop — no other requests can be served."""
    time.sleep(300)
    return {"ok": True}


@mcp.tool()
@_guard
async def hangs_forever_async_guarded() -> dict:
    """Same hang, but cooperatively yields. The event loop stays free to
    service other requests; the question is whether client-side cancellation
    propagates to cancel this coroutine on the server."""
    await asyncio.sleep(300)
    return {"ok": True}


async def _hang_async() -> dict:
    await asyncio.sleep(300)
    return {"ok": True}


# Same hang, but with a 0.5s server-side timeout. Demonstrates that
# _guard's per-tool timeout self-cancels the coroutine even when the
# client never sends notifications/cancelled.
hangs_forever_async_with_timeout_guarded = _guard(_hang_async, timeout_s=0.5)
hangs_forever_async_with_timeout_guarded.__name__ = "hangs_forever_async_with_timeout_guarded"
mcp.tool()(hangs_forever_async_with_timeout_guarded)


# ---- introspection -----------------------------------------------------
@mcp.tool()
@_guard
async def task_count() -> dict:
    """Report the number of asyncio tasks currently alive on the server,
    excluding this call's own task. Used to detect leaked coroutines from
    cancelled prior requests."""
    me = asyncio.current_task()
    alive = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
    return {"count": len(alive)}


# ---- size-guard boundary ------------------------------------------------
@mcp.tool()
@_guard
def payload_of_size_guarded(n: int) -> dict:
    """Returns a payload whose JSON serialization is exactly n+11 bytes.
    Caller chooses n to probe the size guard at, just under, or just over
    MAX_RESPONSE_BYTES."""
    return {"data": "x" * n}


# ---- nesting depth ------------------------------------------------------
@mcp.tool()
@_guard
def deeply_nested_guarded(depth: int) -> Any:
    """Returns a list nested `depth` levels deep. json.dumps uses recursion
    and Python's default recursion limit is 1000."""
    out: Any = []
    inner = out
    for _ in range(depth):
        new: list = []
        inner.append(new)
        inner = new
    return {"nested": out}


# ---- circular reference -------------------------------------------------
@mcp.tool()
@_guard
def circular_ref_guarded() -> Any:
    """Returns a dict with a self-reference. json.dumps should raise
    ValueError('Circular reference detected')."""
    d: dict = {"name": "self"}
    d["me"] = d
    return d


# ---- weird scalar types -------------------------------------------------
@mcp.tool()
@_guard
def returns_nan_guarded() -> dict:
    """json.dumps emits 'NaN' by default — not valid JSON per the spec."""
    return {"value": float("nan")}


@mcp.tool()
@_guard
def returns_bytes_guarded() -> dict:
    """bytes is not JSON-serializable; falls through to default=str."""
    return {"data": b"binary content"}


# ---- unserializable return ---------------------------------------------
class _Exploding:
    def __repr__(self) -> str:
        raise RuntimeError("exploding __repr__")

    __str__ = __repr__


@mcp.tool()
def unserializable_unguarded() -> Any:
    """Returns an object whose __repr__ raises during JSON encoding."""
    return {"bad": {_Exploding()}}


@mcp.tool()
@_guard
def unserializable_guarded() -> Any:
    """Same payload, but the guard's broad except should catch it."""
    return {"bad": {_Exploding()}}


if __name__ == "__main__":
    # Route stress server's own logs to stderr (the MCP transport owns stdout).
    # We deliberately do NOT protect against tool-level `print()` calls — that's
    # exactly what stdout_pollution_guarded is demonstrating.
    import argparse
    import logging
    import os

    logging.basicConfig(stream=sys.stderr, level="INFO")
    from mcp_server.lifecycle import run_with_lifecycle, register_cleanup

    async def _stress_cleanup() -> None:
        # Marker line for restart probes to grep. Fires from FastMCP
        # lifespan __aexit__, so it runs on stdio AND HTTP shutdown.
        print("STRESS_CLEANUP_RAN", file=sys.stderr, flush=True)

    register_cleanup(_stress_cleanup)

    p = argparse.ArgumentParser()
    p.add_argument("--transport", default=os.environ.get("MCP_TRANSPORT", "stdio"),
                   choices=["stdio", "sse", "streamable-http"])
    p.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8765")))
    args = p.parse_args()

    if args.transport == "stdio":
        asyncio.run(run_with_lifecycle(mcp))
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        if args.transport == "sse":
            asyncio.run(mcp.run_sse_async())
        else:
            asyncio.run(mcp.run_streamable_http_async())
