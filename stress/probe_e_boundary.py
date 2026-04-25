"""Probe E: boundary conditions on size guard and serialization.

Tests:
  E1 size guard at exact limit, +1, -1
  E2 deeply nested objects vs Python's recursion limit
  E3 circular references
  E4 NaN, Infinity (technically invalid JSON)
  E5 bytes (caught by default=str fallback)

Run:   python -m stress.probe_e_boundary
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


MAX = int(os.environ.get("MCP_MAX_RESPONSE_BYTES", 256 * 1024))


def _summarize(result) -> str:
    parts = []
    if getattr(result, "isError", False):
        parts.append("isError=True")
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text[:200])
    return " | ".join(parts) if parts else repr(result)[:200]


async def _call(session: ClientSession, tool: str, args: dict, timeout: float = 5.0) -> str:
    try:
        result = await asyncio.wait_for(session.call_tool(tool, args), timeout=timeout)
        return _summarize(result)
    except asyncio.TimeoutError:
        return f"TIMEOUT after {timeout}s"
    except Exception as e:
        return f"ERROR {type(e).__name__}: {str(e)[:150]}"


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "stress.server"])

    # `payload_of_size_guarded(n)` returns {"data": "x" * n}, which json-encodes
    # to exactly n + 11 bytes (the envelope is `{"data": "..."}`).  We hit each
    # interesting boundary precisely.
    n_at = MAX - 11
    n_under = n_at - 1
    n_over = n_at + 1

    cases = [
        ("E1a size exactly at limit",   "payload_of_size_guarded", {"n": n_at}),
        ("E1b size limit - 1",          "payload_of_size_guarded", {"n": n_under}),
        ("E1c size limit + 1",          "payload_of_size_guarded", {"n": n_over}),
        ("E2a nested depth=500",        "deeply_nested_guarded",   {"depth": 500}),
        ("E2b nested depth=2000",       "deeply_nested_guarded",   {"depth": 2000}),
        ("E2c nested depth=10000",      "deeply_nested_guarded",   {"depth": 10000}),
        ("E3 circular reference",       "circular_ref_guarded",    {}),
        ("E4a NaN",                     "returns_nan_guarded",     {}),
        ("E5 bytes",                    "returns_bytes_guarded",   {}),
    ]

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=5.0)

            for label, tool, args in cases:
                verdict = await _call(session, tool, args)
                # Compress whitespace for table output
                short = " ".join(verdict.split())[:90]
                print(f"{label:<30}  {short}")


if __name__ == "__main__":
    asyncio.run(main())
