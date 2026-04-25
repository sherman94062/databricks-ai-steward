"""Health and introspection tool. Useful for supervisors, restart probes,
and operators who want to check the server is alive without parsing stderr."""

from __future__ import annotations

import asyncio
import importlib.metadata

from mcp_server.app import safe_tool
from mcp_server.lifecycle import is_shutting_down, uptime_s


def _version() -> str:
    try:
        return importlib.metadata.version("databricks-ai-steward")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


@safe_tool()
async def health() -> dict:
    """Report server liveness and runtime stats."""
    me = asyncio.current_task()
    in_flight = sum(
        1 for t in asyncio.all_tasks() if t is not me and not t.done()
    )
    shutting_down = is_shutting_down()
    return {
        "status": "shutting_down" if shutting_down else "ok",
        "ready": not shutting_down,
        "version": _version(),
        "uptime_s": round(uptime_s(), 3),
        "in_flight_tasks": in_flight,
    }
