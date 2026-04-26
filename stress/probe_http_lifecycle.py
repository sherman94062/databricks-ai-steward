"""Probe: HTTP graceful shutdown runs cleanup callbacks.

Companion to probe_restart (stdio). Spawns stress.server with
--transport streamable-http, makes a tool call to confirm the server
is serving, then sends SIGTERM and asserts:
  * process exits cleanly (uvicorn handles graceful drain)
  * STRESS_CLEANUP_RAN appears in stderr — the FastMCP lifespan
    __aexit__ fires on uvicorn shutdown, which calls our cleanup
    callbacks the same way it does for stdio

Run:   python -m stress.probe_http_lifecycle
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


PORT = 18768
URL = f"http://127.0.0.1:{PORT}/mcp"
MAX_WAIT_S = 8.0


def _wait_for_port(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


async def _smoke_call() -> bool:
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=5.0)
            r = await session.call_tool("ok_guarded", {})
            return not getattr(r, "isError", False)


def main() -> int:
    env = {**os.environ, "MCP_LOG_LEVEL": "INFO"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "stress.server",
         "--transport", "streamable-http", "--port", str(PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    try:
        if not _wait_for_port(PORT, timeout=5.0):
            print(f"FAIL: server did not bind {PORT} within 5s")
            return 1
        print(f"[server] up on {URL}")

        if not asyncio.run(_smoke_call()):
            print("FAIL: smoke call failed")
            return 1
        print("[smoke] ok_guarded returned cleanly")

        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)

        # Wait for exit
        deadline = t0 + MAX_WAIT_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        elapsed = time.monotonic() - t0

        if proc.poll() is None:
            proc.kill()
            proc.wait(2)
            print(f"FAIL: did not exit within {MAX_WAIT_S}s after SIGTERM")
            return 1

        try:
            stderr = proc.stderr.read().decode(errors="replace")
        except Exception:
            stderr = ""

        cleanup_ran = "STRESS_CLEANUP_RAN" in stderr
        clean_exit = proc.returncode in (0, -signal.SIGTERM, signal.SIGTERM)
        # uvicorn returns 0 on graceful shutdown; on macOS it's sometimes
        # reported as -SIGTERM if the parent observed it that way.

        print()
        print(f"exit_code         {proc.returncode}")
        print(f"elapsed           {elapsed:.2f}s")
        print(f"cleanup ran       {'✓' if cleanup_ran else '✗ MISSING'}")
        print(f"clean exit code   {'✓' if clean_exit else '✗'}")

        if cleanup_ran and clean_exit:
            print()
            print("PASS")
            return 0
        print()
        print("FAIL")
        return 1
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(2)


if __name__ == "__main__":
    sys.exit(main())
