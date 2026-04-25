"""FastMCP instance and shared tool guardrails.

Guards are deliberately conservative because this server runs over stdio: any
uncaught exception kills the process, any write to stdout corrupts the JSON-RPC
stream, and any oversized response blows out the client's context window.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import os
import sys
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

# ---- stdout protection --------------------------------------------------
# FastMCP owns stdout for the MCP protocol. Any library that logs to stdout by
# default will corrupt the session. Pin the root logger to stderr before any
# downstream imports configure their own handlers.
logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("MCP_LOG_LEVEL", "WARNING"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mcp_server")

# ---- response size cap --------------------------------------------------
MAX_RESPONSE_BYTES = int(os.environ.get("MCP_MAX_RESPONSE_BYTES", 256 * 1024))

# ---- per-tool timeout ---------------------------------------------------
# Servers self-cancel a tool that runs longer than this. The MCP Python
# client SDK does not send notifications/cancelled when its call_tool is
# cancelled, so without a server-side cap, an aborted client request
# leaves the tool's coroutine suspended on the server (holding DB
# connections, cursors, locks, etc).
DEFAULT_TOOL_TIMEOUT_S = float(os.environ.get("MCP_TOOL_TIMEOUT_S", "30"))

# ---- FastMCP instance ---------------------------------------------------
mcp = FastMCP("databricks-ai-steward")


def _error(error_type: str, message: str) -> dict:
    return {"error": {"type": error_type, "message": message}}


def _cap_response(result: Any) -> Any:
    try:
        serialized = json.dumps(result, default=str)
    except Exception as e:
        # Broad except is intentional: any failure during serialization
        # (TypeError, ValueError, or arbitrary exceptions raised by objects'
        # __repr__/__str__ during encoding) must become a structured error
        # rather than escape and kill the server.
        log.exception("tool return value is not JSON-serializable")
        return _error("ResponseNotSerializable", f"{type(e).__name__}: {e}")
    if len(serialized) > MAX_RESPONSE_BYTES:
        log.warning(
            "tool response exceeded cap: %d bytes > %d",
            len(serialized),
            MAX_RESPONSE_BYTES,
        )
        return _error(
            "ResponseTooLarge",
            f"Tool returned {len(serialized)} bytes; cap is {MAX_RESPONSE_BYTES}. "
            "Narrow the query (row limit, column projection) or raise "
            "MCP_MAX_RESPONSE_BYTES if the large payload is intentional.",
        )
    return result


def _guard(func: Callable, *, timeout_s: float | None = None) -> Callable:
    """Wrap a tool function so exceptions, oversize returns, and (for async
    tools) timeouts become structured error responses instead of killing the
    server or leaking suspended coroutines."""
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                if timeout_s is None:
                    result = await func(*args, **kwargs)
                else:
                    result = await asyncio.wait_for(
                        func(*args, **kwargs), timeout=timeout_s
                    )
            except asyncio.TimeoutError:
                log.warning("tool exceeded %.1fs timeout: %s", timeout_s, func.__name__)
                return _error(
                    "ToolTimeout",
                    f"tool exceeded {timeout_s}s timeout and was cancelled server-side",
                )
            except Exception as e:
                log.exception("tool raised: %s", func.__name__)
                return _error(type(e).__name__, str(e))
            return _cap_response(result)

        return wrapper

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            result = func(*args, **kwargs)
        except Exception as e:
            log.exception("tool raised: %s", func.__name__)
            return _error(type(e).__name__, str(e))
        return _cap_response(result)

    return wrapper


def safe_tool(
    *tool_args,
    timeout_s: float | None = DEFAULT_TOOL_TIMEOUT_S,
    allow_sync: bool = False,
    **tool_kwargs,
):
    """Drop-in replacement for @mcp.tool() that applies the shared guards.

    By default rejects synchronous tools: a single sync I/O call wedges
    all other concurrent calls on the session, prevents clean shutdown on
    client disconnect, and blocks SIGINT. Make tools `async def` and use
    `asyncio.to_thread(...)` for blocking I/O. Pass `allow_sync=True` only
    for pure-CPU work that returns quickly.

    Use this for any new tool. Raw @mcp.tool() still works but skips the
    guards — only reach for it if you have a specific reason.
    """

    def decorator(func: Callable) -> Callable:
        if not inspect.iscoroutinefunction(func) and not allow_sync:
            raise TypeError(
                f"safe_tool: {func.__name__!r} is synchronous. Sync tools "
                f"block the asyncio event loop, breaking concurrency, "
                f"shutdown, and cancellation. Make it `async def` and wrap "
                f"any blocking I/O in asyncio.to_thread(...). Pass "
                f"allow_sync=True only if the tool is pure-CPU and fast."
            )
        guarded = _guard(func, timeout_s=timeout_s)
        return mcp.tool(*tool_args, **tool_kwargs)(guarded)

    return decorator
