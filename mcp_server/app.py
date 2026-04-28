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
from collections.abc import Callable
from typing import Any

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
    server or leaking suspended coroutines.

    Also: rate-limits per (tool, caller_id) and emits an audit record at
    start + end of every call. Both are cross-cutting concerns that
    every tool needs uniformly; the alternative (per-tool wiring) drifts.
    """
    # Local imports to avoid pulling audit/rate_limit at app.py module
    # load time — keeps app.py importable from contexts that don't want
    # those side effects (e.g. some unit tests).
    from mcp_server import audit, rate_limit, telemetry

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            global _in_flight_tools
            _in_flight_tools += 1
            telemetry.in_flight_inc()
            tool_name = func.__name__
            request_id = audit.new_request_id()
            caller = audit.current_caller_id()
            audit.emit_tool_start(tool_name, request_id, args, kwargs)
            # Make the tool name visible to downstream code (e.g.
            # databricks.client.get_workspace) so it can pick the
            # right per-tool credential. The contextvar token must be
            # reset before the wrapper returns, which happens in the
            # `finally` block below.
            tool_ctx_token = audit.set_current_tool(tool_name)
            t0 = asyncio.get_event_loop().time()

            # 1. Rate-limit gate. Charged before the tool runs so that
            #    a runaway caller can't exceed quota by N concurrent
            #    in-flight calls; the bucket is incremented on entry,
            #    not on completion.
            try:
                await rate_limit.check(tool_name, caller)
            except rate_limit.RateLimitExceeded as e:
                audit.emit_rate_limit_exceeded(
                    tool_name, request_id, e.limit.count, e.limit.window_s,
                )
                resp = _error(
                    "RateLimitExceeded",
                    f"rate limit: {e.limit.count} calls per {e.limit.window_s}s "
                    f"for {tool_name!r} per caller",
                )
                latency_s = asyncio.get_event_loop().time() - t0
                audit.emit_tool_end(
                    tool_name, request_id, latency_s * 1000,
                    outcome="rate_limited",
                    error_type="RateLimitExceeded",
                )
                telemetry.record_tool_call(tool_name, caller, "rate_limited", latency_s)
                _in_flight_tools -= 1
                telemetry.in_flight_dec()
                audit.reset_current_tool(tool_ctx_token)
                return resp

            with telemetry.tool_span(tool_name, request_id, caller):
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
                                latency_s = asyncio.get_event_loop().time() - t0
                                audit.emit_tool_end(
                                    tool_name, request_id, latency_s * 1000,
                                    outcome="timeout", error_type="ToolTimeout",
                                )
                                telemetry.record_tool_call(
                                    tool_name, caller, "timeout", latency_s,
                                )
                                return _error(
                                    "ToolTimeout",
                                    f"tool exceeded {timeout_s}s timeout and was cancelled server-side",
                                )
                            raise  # tool itself raised TimeoutError — let the generic handler classify
                except Exception as e:
                    log.exception("tool raised: %s", func.__name__)
                    resp = _error(type(e).__name__, str(e))
                    latency_s = asyncio.get_event_loop().time() - t0
                    audit.emit_tool_end(
                        tool_name, request_id, latency_s * 1000,
                        outcome="error", error_type=type(e).__name__,
                    )
                    telemetry.record_tool_call(tool_name, caller, "error", latency_s)
                    return resp
                else:
                    capped = _cap_response(result)
                    latency_s = asyncio.get_event_loop().time() - t0
                    error_type = None
                    if isinstance(capped, dict) and "error" in capped:
                        err = capped["error"]
                        if isinstance(err, dict):
                            error_type = err.get("type")
                    outcome = "error" if error_type else "success"
                    response_bytes = None
                    try:
                        response_bytes = len(json.dumps(capped, default=str))
                    except Exception:  # nosec B110 — audit-only, swallowed deliberately
                        # response_bytes is best-effort metadata for the
                        # audit log. If the (already-capped) payload still
                        # can't be re-serialized — which can happen for
                        # exotic types — drop the field rather than fail
                        # the tool call.
                        pass
                    audit.emit_tool_end(
                        tool_name, request_id, latency_s * 1000,
                        outcome=outcome, error_type=error_type,
                        response_bytes=response_bytes,
                    )
                    telemetry.record_tool_call(tool_name, caller, outcome, latency_s)
                    return capped
                finally:
                    _in_flight_tools -= 1
                    telemetry.in_flight_dec()
                    audit.reset_current_tool(tool_ctx_token)

        return wrapper

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
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

    return sync_wrapper


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
