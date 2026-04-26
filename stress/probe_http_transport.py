"""Probe: HTTP transport works end-to-end.

Spawns the production server with --transport streamable-http on a chosen
port, polls until the port is open, then uses the MCP SDK's
streamablehttp_client to:
  1. initialize the session
  2. list tools
  3. call list_catalogs
  4. call health and verify ready=True

Tears the server down with SIGTERM (uvicorn handles graceful shutdown
natively for HTTP transports).

Run:   python -m stress.probe_http_transport
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


PORT = 18765   # avoid collision with default 8765
URL = f"http://127.0.0.1:{PORT}/mcp"


def _wait_for_port(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _summarize(result) -> str:
    parts = []
    if getattr(result, "isError", False):
        parts.append("isError=True")
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text[:300])
    return " | ".join(parts) if parts else repr(result)[:200]


async def _exercise() -> int:
    async with streamablehttp_client(URL) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=5.0)
            print("[init] ok")

            tools = await session.list_tools()
            tool_names = sorted(t.name for t in tools.tools)
            print(f"[tools] {tool_names}")
            if "list_catalogs" not in tool_names or "health" not in tool_names:
                print(f"FAIL: expected list_catalogs and health in tools, got {tool_names}")
                return 1

            r = await session.call_tool("list_catalogs", {})
            # Parse the raw text content (not the truncated _summarize output).
            raw_text = next(
                (item.text for item in getattr(r, "content", []) or [] if getattr(item, "text", None)),
                "",
            )
            try:
                payload = json.loads(raw_text)
                catalogs = payload.get("catalogs", [])
                ok = (
                    isinstance(catalogs, list)
                    and len(catalogs) > 0
                    and all(isinstance(c, dict) and "name" in c for c in catalogs)
                )
            except (json.JSONDecodeError, AttributeError):
                ok = False
            print(f"[list_catalogs] {len(catalogs) if ok else 0} catalog(s)")
            if not ok:
                print("FAIL: list_catalogs did not return the expected [{name,...}] shape")
                return 1

            r = await session.call_tool("health", {})
            summary = _summarize(r)
            print(f"[health] {summary}")
            try:
                health_payload = json.loads(summary)
            except json.JSONDecodeError:
                print("FAIL: health response was not JSON")
                return 1
            if not health_payload.get("ready"):
                print(f"FAIL: health.ready was {health_payload.get('ready')!r}")
                return 1

    return 0


def main() -> int:
    env = {**os.environ, "MCP_LOG_LEVEL": "WARNING"}
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "mcp_server.server",
            "--transport", "streamable-http",
            "--port", str(PORT),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    try:
        if not _wait_for_port(PORT, timeout=5.0):
            print(f"FAIL: server did not bind {PORT} within 5s")
            return 1
        print(f"[server] up on {URL}")

        result = asyncio.run(_exercise())
        if result != 0:
            return result

        print()
        print("PASS")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(2.0)


if __name__ == "__main__":
    sys.exit(main())
