"""A deliberately-misbehaving MCP server for fault injection testing.

Pairs each pathological behavior with two variants:
  * _unguarded — registered with plain @mcp.tool() (no protection)
  * _guarded   — registered with our production _guard wrapper

Running as an MCP server over stdio; driven by stress.harness. Not imported
by the production server.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from mcp_server.app import _guard  # reusing the production guard logic

mcp = FastMCP("stress-test")


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
    """Sleeps far longer than any client should wait. No timeout guard exists."""
    time.sleep(300)
    return {"ok": True}


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
    import logging

    logging.basicConfig(stream=sys.stderr, level="WARNING")
    mcp.run()
