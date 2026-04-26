"""Health and introspection tool. Useful for supervisors, restart probes,
and operators who want to check the server is alive without parsing stderr."""

from __future__ import annotations

import importlib.metadata

from mcp_server.app import in_flight_tool_count, safe_tool
from mcp_server.lifecycle import is_shutting_down, uptime_s


def _version() -> str:
    try:
        return importlib.metadata.version("databricks-ai-steward")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


@safe_tool()
async def health() -> dict:
    """Report server liveness and runtime stats.

    `in_flight_tools` counts tool calls currently executing — not
    asyncio.all_tasks(), which would include transport workers
    (uvicorn / anyio internals). Subtracts 1 for this call's own slot,
    which has already incremented the counter via _guard.
    """
    shutting_down = is_shutting_down()
    return {
        "status": "shutting_down" if shutting_down else "ok",
        "ready": not shutting_down,
        "version": _version(),
        "uptime_s": round(uptime_s(), 3),
        "in_flight_tools": max(0, in_flight_tool_count() - 1),
    }
