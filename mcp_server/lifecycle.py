"""Graceful shutdown and process lifecycle for the MCP server.

Two layers:

  * `lifespan(server)` — a FastMCP lifespan context manager. Both
    transports invoke it: stdio enters it inside `run_stdio_async`,
    HTTP transports invoke it via uvicorn's lifespan event. Cleanup
    callbacks registered via `register_cleanup` run on `__aexit__`,
    each capped at `MCP_CLEANUP_TIMEOUT_S` (default 2s).

  * `run_with_lifecycle(mcp)` — stdio-only signal handler. Catches
    SIGTERM/SIGINT and closes stdin to force the anyio reader thread
    to release (asyncio cancellation can't reach blocked threads).
    The lifespan __aexit__ then handles cleanup naturally.

For HTTP, uvicorn already does graceful drain on SIGTERM and invokes
the lifespan shutdown after; we don't install our own signal handlers
on that path to avoid double-shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

# Use our own logger rather than importing from mcp_server.app — app.py
# imports lifespan from this module, which would create a circular
# import. mcp_server.app's basicConfig has already configured the root
# logger (it's imported before this module by both server.py and stress).
log = logging.getLogger("mcp_server.lifecycle")


SHUTDOWN_GRACE_S = float(os.environ.get("MCP_SHUTDOWN_GRACE_S", "5"))
CLEANUP_TIMEOUT_S = float(os.environ.get("MCP_CLEANUP_TIMEOUT_S", "2"))

_shutdown_event: asyncio.Event | None = None
_cleanup_callbacks: list[Callable[[], Awaitable[None]]] = []
_started_at: float = time.monotonic()
_shutting_down: bool = False


def register_cleanup(fn: Callable[[], Awaitable[None]]) -> None:
    """Register an async cleanup callback to run on graceful shutdown.

    Each callback is awaited with a per-callback timeout (CLEANUP_TIMEOUT_S).
    Exceptions are logged and swallowed — one bad callback should not block
    others from running.

    Callbacks fire on both stdio and HTTP shutdown paths because they
    are invoked from the FastMCP lifespan __aexit__, which both
    transports run.
    """
    _cleanup_callbacks.append(fn)


def is_shutting_down() -> bool:
    """True once shutdown has begun.

    On stdio: flips when SIGTERM/SIGINT is received (before drain).
    On HTTP: flips at the start of the lifespan shutdown phase (after
    uvicorn drains in-flight requests). The HTTP latency is intrinsic
    to uvicorn — we'd need to install a signal handler that races
    uvicorn's to flip earlier, which is brittle.
    """
    return _shutting_down


def uptime_s() -> float:
    return time.monotonic() - _started_at


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[None]:
    """FastMCP lifespan: runs cleanup callbacks on shutdown for both
    stdio and HTTP transports.

    Pass this as `lifespan=lifespan` when constructing FastMCP.
    """
    global _started_at, _shutting_down
    _started_at = time.monotonic()
    _shutting_down = False
    try:
        yield None
    finally:
        _shutting_down = True
        if _cleanup_callbacks:
            log.warning("running %d cleanup callback(s)", len(_cleanup_callbacks))
            for cb in _cleanup_callbacks:
                try:
                    await asyncio.wait_for(cb(), timeout=CLEANUP_TIMEOUT_S)
                except TimeoutError:
                    log.warning("cleanup callback %s exceeded %.1fs",
                                getattr(cb, "__name__", repr(cb)), CLEANUP_TIMEOUT_S)
                except Exception:
                    log.exception("cleanup callback %s raised",
                                  getattr(cb, "__name__", repr(cb)))


async def run_with_lifecycle(mcp: FastMCP) -> None:
    """Run mcp.run_stdio_async with cooperative SIGTERM/SIGINT handling.

    HTTP transports do not use this — they go directly through
    `mcp.run_streamable_http_async()` / `run_sse_async()` and rely on
    uvicorn's built-in graceful drain.
    """
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()

    def _on_signal(signame: str) -> None:
        global _shutting_down
        if _shutdown_event.is_set():
            log.warning("received %s during shutdown; ignoring", signame)
            return
        log.warning("received %s; beginning graceful shutdown (grace=%.1fs)",
                    signame, SHUTDOWN_GRACE_S)
        _shutdown_event.set()
        # Flip is_shutting_down() *before* drain so the health tool can
        # advertise drain mode immediately.
        _shutting_down = True
        # FastMCP's stdio_server runs stdin_reader on anyio, which under
        # asyncio dispatches the blocking read to a worker thread.
        # asyncio cancellation cannot reach a blocked thread; closing
        # stdin forces EOF, the reader thread returns, the anyio task
        # group completes, and run_stdio_async exits naturally.
        try:
            os.close(sys.stdin.fileno())
        except OSError:
            pass

    for sig, name in [(signal.SIGTERM, "SIGTERM"), (signal.SIGINT, "SIGINT")]:
        try:
            loop.add_signal_handler(sig, _on_signal, name)
        except NotImplementedError:
            # Windows: add_signal_handler isn't supported. Fall back.
            # Bind `name` via default-arg so each handler captures its
            # own value (otherwise both would see the loop's last value).
            log.warning("loop.add_signal_handler unavailable for %s; "
                        "using signal.signal fallback", name)
            signal.signal(
                sig,
                lambda *_, _n=name: loop.call_soon_threadsafe(_on_signal, _n),
            )

    server_task = asyncio.create_task(mcp.run_stdio_async(), name="mcp-server")
    shutdown_watch = asyncio.create_task(_shutdown_event.wait(), name="shutdown-watch")

    done, _ = await asyncio.wait(
        {server_task, shutdown_watch},
        return_when=asyncio.FIRST_COMPLETED,
    )

    if shutdown_watch in done:
        log.warning("cancelling server task")
        server_task.cancel()
        try:
            await asyncio.wait_for(server_task, timeout=SHUTDOWN_GRACE_S)
            log.warning("server cancelled cleanly")
        except asyncio.CancelledError:
            log.warning("server cancelled cleanly")
        except TimeoutError:
            log.warning("server did not stop within %.1fs grace; abandoning",
                        SHUTDOWN_GRACE_S)
    else:
        log.info("server exited; client likely disconnected")
        shutdown_watch.cancel()
        try:
            await shutdown_watch
        except asyncio.CancelledError:
            pass

    # Cleanup callbacks now run via the FastMCP lifespan __aexit__,
    # not here. Both stdio and HTTP get the same path.
