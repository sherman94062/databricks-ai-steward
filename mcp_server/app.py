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

# ---- in-flight tool counter ---------------------------------------------
# Counts tool tasks specifically, not asyncio.all_tasks() which would
# include uvicorn workers, FastMCP internals, etc. Incremented in _guard
# on tool entry, decremented in finally on exit. Single-threaded
# asyncio means no lock needed.
_in_flight_tools: int = 0


def in_flight_tool_count() -> int:
    return _in_flight_tools

# ---- FastMCP instance ---------------------------------------------------
# Lazy import to avoid circular: lifecycle imports nothing from us, so
# this is safe.
from mcp_server.lifecycle import lifespan as _lifespan  # noqa: E402

mcp = FastMCP("databricks-ai-steward", lifespan=_lifespan)


def _scrub(message: str) -> str:
    """Redact known secrets from a string before it leaves the process.

    Reads DATABRICKS_HOST, DATABRICKS_TOKEN, and MCP_BEARER_TOKEN from
    the environment at call time so .env reloads pick up new values.
    Defense in depth: SDK errors *shouldn't* embed credentials, but if
    one ever does, the structured-error path is the last gate before
    the message reaches the MCP client.
    """
    if not isinstance(message, str):
        return message
    for var in ("DATABRICKS_TOKEN", "MCP_BEARER_TOKEN", "DATABRICKS_HOST"):
        secret = os.environ.get(var, "").strip()
        if secret and len(secret) >= 8:  # avoid scrubbing trivially short values
            message = message.replace(secret, f"<redacted:{var}>")
    return message


def _error(error_type: str, message: str) -> dict:
    return {"error": {"type": error_type, "message": _scrub(message)}}


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
            global _in_flight_tools
            _in_flight_tools += 1
            try:
                if timeout_s is None:
                    result = await func(*args, **kwargs)
                else:
                    # asyncio.timeout's `cm.expired()` is the only reliable way
                    # to distinguish *our* deadline from a TimeoutError raised
                    # by the tool itself (e.g. databricks-sdk's OperationTimeout
                    # subclasses TimeoutError). Without this, every SDK timeout
                    # would be misreported as our ToolTimeout.
                    try:
                        async with asyncio.timeout(timeout_s) as cm:
                            result = await func(*args, **kwargs)
                    except TimeoutError:
                        if cm.expired():
                            log.warning(
                                "tool exceeded %.1fs timeout: %s",
                                timeout_s, func.__name__,
                            )
                            return _error(
                                "ToolTimeout",
                                f"tool exceeded {timeout_s}s timeout and was cancelled server-side",
                            )
                        raise  # tool itself raised TimeoutError — let the generic handler classify
            except Exception as e:
                log.exception("tool raised: %s", func.__name__)
                return _error(type(e).__name__, str(e))
            else:
                return _cap_response(result)
            finally:
                _in_flight_tools -= 1

        return wrapper

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        global _in_flight_tools
        _in_flight_tools += 1
        try:
            result = func(*args, **kwargs)
        except Exception as e:
            log.exception("tool raised: %s", func.__name__)
            return _error(type(e).__name__, str(e))
        else:
            return _cap_response(result)
        finally:
            _in_flight_tools -= 1

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
