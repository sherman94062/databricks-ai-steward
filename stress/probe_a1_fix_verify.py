"""Probe A.1 verification: per-tool server-side timeout cleans up leaked tasks.

Same shape as probe_a1_leak, but pointed at hangs_forever_async_with_timeout_guarded
(timeout_s=0.5 inside the guard). After 50 client-cancelled calls, give the
server 1s to self-cancel, then check task_count.

Expected: count returns to ~baseline because every leaked task self-cancels
0.5s after the client gives up.

Run:   python -m stress.probe_a1_fix_verify
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
                        session.call_tool(
                            "hangs_forever_async_with_timeout_guarded", {},
                        ),
                        timeout=0.1,
                    )
                except asyncio.TimeoutError:
                    pass

            after = await _count(session)
            print(f"[immediately after {N} cancelled hangs] task_count = {after}",
                  file=sys.stderr)

            # Server-side timeout is 0.5s; wait long enough for all to self-cancel
            await asyncio.sleep(1.0)
            settled = await _count(session)
            print(f"[after 1.0s settle] task_count = {settled}", file=sys.stderr)

            verdict = "FIXED" if settled <= baseline + 1 else "STILL LEAKING"
            print(f"\n→ {verdict} (delta from baseline: {settled - baseline})",
                  file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
