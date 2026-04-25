"""Graceful shutdown and process lifecycle for the MCP stdio server.

FastMCP's default `run()` does not handle SIGTERM/SIGINT cooperatively
with in-flight tools — the process exits abruptly and tools that hold
external resources (DB connections, cursors, locks) cannot release them.

`run_with_lifecycle(mcp)` replaces `mcp.run()` and adds:
  * SIGTERM and SIGINT handlers that trigger a bounded graceful shutdown
  * a registry of async cleanup callbacks that run before the process exits
  * a `is_shutting_down()` flag for the health tool

Shutdown sequence on signal:
  1. handler sets the shutdown event
  2. server task is cancelled (which cascades to in-flight tool tasks)
  3. wait for server to exit, up to MCP_SHUTDOWN_GRACE_S seconds
  4. run each cleanup callback, each capped at 2s
  5. return — the asyncio.run wrapper returns to caller

If the grace deadline expires, the partially-cancelled server is
abandoned and shutdown proceeds anyway. The caller should rely on
process exit (return from main) rather than calling sys.exit, so cleanup
callbacks are reached even on grace overrun.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from typing import Awaitable, Callable

from mcp.server.fastmcp import FastMCP

from mcp_server.app import log


SHUTDOWN_GRACE_S = float(os.environ.get("MCP_SHUTDOWN_GRACE_S", "5"))
CLEANUP_TIMEOUT_S = float(os.environ.get("MCP_CLEANUP_TIMEOUT_S", "2"))

_shutdown_event: asyncio.Event | None = None
_cleanup_callbacks: list[Callable[[], Awaitable[None]]] = []
_started_at: float = time.monotonic()


def register_cleanup(fn: Callable[[], Awaitable[None]]) -> None:
    """Register an async cleanup callback to run before exit on graceful shutdown.

    Each callback is awaited with a per-callback timeout (CLEANUP_TIMEOUT_S).
    Exceptions are logged and swallowed — one bad callback should not block
    others from running.
    """
    _cleanup_callbacks.append(fn)


def is_shutting_down() -> bool:
    """True once a shutdown signal has been received."""
    return _shutdown_event is not None and _shutdown_event.is_set()


def uptime_s() -> float:
    return time.monotonic() - _started_at


async def run_with_lifecycle(mcp: FastMCP) -> None:
    """Run mcp.run_stdio_async with cooperative SIGTERM/SIGINT handling."""
    global _shutdown_event, _started_at
    _shutdown_event = asyncio.Event()
    _started_at = time.monotonic()

    loop = asyncio.get_running_loop()

    def _on_signal(signame: str) -> None:
        if _shutdown_event.is_set():
            log.warning("received %s during shutdown; ignoring", signame)
            return
        # WARNING level so shutdown events show up under default MCP_LOG_LEVEL.
        log.warning("received %s; beginning graceful shutdown (grace=%.1fs)",
                    signame, SHUTDOWN_GRACE_S)
        _shutdown_event.set()
        # FastMCP's stdio_server runs stdin_reader on anyio, which under
        # asyncio dispatches the blocking read to a worker thread. asyncio
        # cancellation cannot reach a blocked thread; closing stdin forces
        # EOF, the reader thread returns, the anyio task group completes,
        # and run_stdio_async exits naturally. Without this the server
        # never observes our cancellation and shutdown hangs.
        try:
            os.close(sys.stdin.fileno())
        except OSError:
            pass

    for sig, name in [(signal.SIGTERM, "SIGTERM"), (signal.SIGINT, "SIGINT")]:
        try:
            loop.add_signal_handler(sig, _on_signal, name)
        except NotImplementedError:
            # Windows: add_signal_handler isn't supported. Fall back to
            # signal.signal which fires on the main thread; we use
            # call_soon_threadsafe to bridge into the loop.
            log.warning("loop.add_signal_handler unavailable for %s; "
                        "using signal.signal fallback", name)
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(_on_signal, name))

    server_task = asyncio.create_task(mcp.run_stdio_async(), name="mcp-server")
    shutdown_watch = asyncio.create_task(_shutdown_event.wait(), name="shutdown-watch")

    done, _ = await asyncio.wait(
        {server_task, shutdown_watch},
        return_when=asyncio.FIRST_COMPLETED,
    )

    if shutdown_watch in done:
        # Signal-driven shutdown
        log.warning("cancelling server task")
        server_task.cancel()
        try:
            await asyncio.wait_for(server_task, timeout=SHUTDOWN_GRACE_S)
            log.warning("server stopped cleanly within grace")
        except asyncio.CancelledError:
            log.warning("server cancelled cleanly")
        except asyncio.TimeoutError:
            log.warning("server did not stop within %.1fs grace; abandoning",
                        SHUTDOWN_GRACE_S)
    else:
        # Server exited on its own (typically stdin EOF from client disconnect)
        log.info("server exited; client likely disconnected")
        shutdown_watch.cancel()
        try:
            await shutdown_watch
        except asyncio.CancelledError:
            pass

    if _cleanup_callbacks:
        log.info("running %d cleanup callback(s)", len(_cleanup_callbacks))
        for cb in _cleanup_callbacks:
            try:
                await asyncio.wait_for(cb(), timeout=CLEANUP_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning("cleanup callback %s exceeded %.1fs",
                            getattr(cb, "__name__", repr(cb)), CLEANUP_TIMEOUT_S)
            except Exception:
                log.exception("cleanup callback %s raised",
                              getattr(cb, "__name__", repr(cb)))
