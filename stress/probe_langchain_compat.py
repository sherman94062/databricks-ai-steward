"""Probe: compatibility with langchain-mcp-adapters.

`langchain-mcp-adapters` is the official LangChain bridge that wraps an
MCP server's tools as LangChain `BaseTool` instances. Most agent
frameworks built on LangChain (and LangGraph) consume MCP through this
package, so a pass here covers a large slice of the agent ecosystem.

We test the public entry point — `MultiServerMCPClient.get_tools()` —
on both stdio and streamable-http, then invoke each discovered tool
directly via its `ainvoke()` method (no LLM call needed; the adapter
exposes tools as plain awaitable callables).

Run:   python -m stress.probe_langchain_compat
Requires: langchain-mcp-adapters (`pip install -e '.[dev]'` covers it).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient


HTTP_PORT = 18772


def _wait_for_port(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}{('  — ' + detail) if detail else ''}")
    return ok


def _parse_tool_output(out: Any) -> dict | None:
    """langchain-mcp-adapters returns the raw MCP content list:
    `[{'type': 'text', 'text': '<json>', 'id': '...'}]`. Pull the first
    text item and parse it as JSON."""
    if isinstance(out, list) and out and isinstance(out[0], dict):
        text = out[0].get("text")
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return None
    if isinstance(out, str):
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None
    if isinstance(out, dict):
        return out
    return None


async def _exercise(client: MultiServerMCPClient, transport_label: str) -> bool:
    print(f"[{transport_label}]")
    tools = await client.get_tools()
    names = sorted(t.name for t in tools)
    a = _check(
        "get_tools()",
        names == ["health", "list_catalogs"],
        f"got {names}",
    )

    by_name = {t.name: t for t in tools}

    out = await by_name["list_catalogs"].ainvoke({})
    parsed = _parse_tool_output(out)
    b = _check(
        "list_catalogs.ainvoke()",
        parsed is not None
        and parsed.get("catalogs") == ["main", "analytics", "system"],
        f"got {parsed}",
    )

    out = await by_name["health"].ainvoke({})
    parsed = _parse_tool_output(out)
    c = _check(
        "health.ainvoke()",
        parsed is not None
        and parsed.get("ready") is True
        and parsed.get("status") == "ok",
        f"ready={None if parsed is None else parsed.get('ready')}",
    )

    return a and b and c


async def _test_stdio() -> bool:
    client = MultiServerMCPClient({
        "databricks-steward": {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "mcp_server.server"],
        },
    })
    return await _exercise(client, "stdio")


async def _test_http() -> bool:
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_server.server",
         "--transport", "streamable-http", "--port", str(HTTP_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "MCP_LOG_LEVEL": "WARNING"},
    )
    try:
        if not _wait_for_port(HTTP_PORT, timeout=5.0):
            print(f"FAIL: HTTP server did not bind {HTTP_PORT}")
            return False
        client = MultiServerMCPClient({
            "databricks-steward": {
                "transport": "streamable_http",
                "url": f"http://127.0.0.1:{HTTP_PORT}/mcp",
            },
        })
        return await _exercise(client, "streamable-http")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(2)


async def main() -> int:
    stdio_ok = await _test_stdio()
    print()
    http_ok = await _test_http()
    print()
    if stdio_ok and http_ok:
        print("PASS — langchain-mcp-adapters compatibility verified on stdio and streamable-http")
        return 0
    print("FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
