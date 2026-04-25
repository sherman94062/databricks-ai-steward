"""Probe A.1: confirm that cancelled async tool calls leak server-side tasks.

Procedure:
  1. Open session, call task_count for a baseline.
  2. Loop N times: kick off hangs_forever_async_guarded with a 100ms timeout,
     swallow the TimeoutError.
  3. Call task_count again. If tasks accumulated, the delta will be ≈ N
     (server-side coroutines for the cancelled requests are still suspended).

Run:   python -m stress.probe_a1_leak
"""

from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def _count(session: ClientSession) -> int:
    result = await asyncio.wait_for(session.call_tool("task_count", {}), timeout=2.0)
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            return json.loads(text)["count"]
    raise RuntimeError(f"unexpected response: {result}")


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "stress.server"])

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=5.0)

            baseline = await _count(session)
            print(f"[baseline] task_count = {baseline}", file=sys.stderr)

            N = 50
            for _ in range(N):
                try:
                    await asyncio.wait_for(
                        session.call_tool("hangs_forever_async_guarded", {}),
                        timeout=0.1,
                    )
                except asyncio.TimeoutError:
                    pass

            after = await _count(session)
            print(f"[after {N} cancelled hangs] task_count = {after}", file=sys.stderr)
            print(f"[delta] {after - baseline} additional tasks", file=sys.stderr)

            # Re-check after a brief pause in case there's lazy GC
            await asyncio.sleep(0.5)
            settled = await _count(session)
            print(f"[after 0.5s settle] task_count = {settled}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
