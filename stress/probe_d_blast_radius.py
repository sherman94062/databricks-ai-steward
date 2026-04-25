"""Probe D: blast radius of a single slow tool call.

While one slow tool is in flight, fire 50 fast (ok_guarded) calls concurrently
on the *same session* and measure their latency distribution. Compares the
sync hang (blocks the loop) vs the async hang (yields the loop).

Run:   python -m stress.probe_d_blast_radius
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def _fast_call(session: ClientSession) -> tuple[bool, float]:
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(session.call_tool("ok_guarded", {}), timeout=3.0)
        return True, time.monotonic() - t0
    except asyncio.TimeoutError:
        return False, time.monotonic() - t0
    except Exception:
        return False, time.monotonic() - t0


async def _baseline_run() -> tuple[list[bool], list[float]]:
    """50 fast calls with no concurrent slow call."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "stress.server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=5.0)
            results = await asyncio.gather(*(_fast_call(session) for _ in range(50)))
    return [r[0] for r in results], [r[1] for r in results]


async def _trial(slow_tool: str) -> tuple[list[bool], list[float]]:
    """50 fast calls while one slow_tool is hanging."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "stress.server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=5.0)

            # Kick off the slow call in the background (don't await it).
            slow_task = asyncio.create_task(session.call_tool(slow_tool, {}))
            # Give the server a beat to start it.
            await asyncio.sleep(0.1)

            try:
                results = await asyncio.gather(*(_fast_call(session) for _ in range(50)))
            finally:
                slow_task.cancel()
                try:
                    await slow_task
                except (asyncio.CancelledError, Exception):
                    pass
    return [r[0] for r in results], [r[1] for r in results]


def _summarize(label: str, ok_flags: list[bool], latencies_s: list[float]) -> None:
    succeeded = [l * 1000 for l, ok in zip(latencies_s, ok_flags) if ok]
    n_ok = sum(ok_flags)
    print(f"\n--- {label} ---")
    print(f"success: {n_ok}/{len(ok_flags)}")
    if succeeded:
        succeeded.sort()
        print(f"p50: {succeeded[len(succeeded) // 2]:.1f} ms")
        print(f"p95: {succeeded[int(len(succeeded) * 0.95)]:.1f} ms")
        print(f"max: {succeeded[-1]:.1f} ms")
        print(f"mean: {statistics.fmean(succeeded):.1f} ms")


async def main() -> None:
    print("[run] baseline (no concurrent slow call)", file=sys.stderr)
    base = await _baseline_run()
    _summarize("baseline", *base)

    print("\n[run] async-slow in flight (hangs_forever_async_guarded)", file=sys.stderr)
    async_t = await _trial("hangs_forever_async_guarded")
    _summarize("with async-slow in flight", *async_t)

    print("\n[run] sync-slow in flight (hangs_forever_guarded)", file=sys.stderr)
    try:
        sync_t = await asyncio.wait_for(
            _trial("hangs_forever_guarded"), timeout=10.0,
        )
        _summarize("with sync-slow in flight", *sync_t)
    except asyncio.TimeoutError:
        print("\n--- with sync-slow in flight ---")
        print("ALL 50 fast calls deadlocked — harness gave up after 10s")


if __name__ == "__main__":
    asyncio.run(main())
