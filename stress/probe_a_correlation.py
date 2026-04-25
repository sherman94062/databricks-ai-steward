"""Probe A: response correlation after a client-side timeout.

Question: when asyncio.wait_for cancels an in-flight call_tool, does the
MCP Python SDK correctly correlate the *next* response on the same session
to the *next* request? Or does the abandoned request's eventual reply leak
into a later caller?

Procedure:
  1. Open a session against stress.server.
  2. Call hangs_forever_guarded with a 1.0s asyncio.wait_for timeout.
  3. Catch the TimeoutError.
  4. Immediately call ok_guarded — must return {"ok": True}.
  5. Repeat the second call a few times to see if state is healed or stuck.

Run:   python -m stress.probe_a_correlation
"""

from __future__ import annotations

import asyncio
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _summarize(result) -> str:
    parts = []
    if getattr(result, "isError", False):
        parts.append("isError=True")
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text[:200])
    return " | ".join(parts) if parts else repr(result)[:200]


async def _trial(hang_tool: str) -> str:
    """Run one trial against a given hanging tool. Returns a one-line verdict."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "stress.server"])

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=5.0)

            # Step 1: kick off the hang with a short timeout
            try:
                await asyncio.wait_for(session.call_tool(hang_tool, {}), timeout=1.0)
                return "UNEXPECTED: hang returned"
            except asyncio.TimeoutError:
                pass

            # Step 2: follow-up call on the same session
            t0 = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    session.call_tool("ok_guarded", {}),
                    timeout=2.0,
                )
                return f"recovered in {(time.monotonic() - t0) * 1000:.1f}ms — {_summarize(result)}"
            except asyncio.TimeoutError:
                return f"WEDGED — follow-up timed out after {time.monotonic() - t0:.2f}s"
            except Exception as e:
                return f"ERROR — {type(e).__name__}: {e}"


async def main() -> int:
    for hang_tool in ("hangs_forever_guarded", "hangs_forever_async_guarded"):
        print(f"[trial] hang_tool={hang_tool}", file=sys.stderr)
        try:
            verdict = await asyncio.wait_for(_trial(hang_tool), timeout=8.0)
        except asyncio.TimeoutError:
            verdict = "harness timed out (subprocess teardown likely blocked)"
        print(f"  → {verdict}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
