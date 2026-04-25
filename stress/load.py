"""Concurrency load harness for the production MCP server.

Opens one session against mcp_server.server, fires M total tool calls with
at most N in flight at a time, and reports latency percentiles + throughput.

Run:   python -m stress.load --concurrent 10 --total 1000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass
class CallResult:
    ok: bool
    latency_s: float
    detail: str  # short description when not ok


async def _one_call(session: ClientSession, tool: str, args: dict) -> CallResult:
    t0 = time.monotonic()
    try:
        result = await session.call_tool(tool, args)
    except Exception as e:
        return CallResult(ok=False, latency_s=time.monotonic() - t0,
                          detail=f"{type(e).__name__}: {str(e)[:120]}")
    elapsed = time.monotonic() - t0
    if getattr(result, "isError", False):
        return CallResult(ok=False, latency_s=elapsed, detail="isError=True")
    return CallResult(ok=True, latency_s=elapsed, detail="")


async def run_load(tool: str, tool_args: dict, concurrent: int, total: int) -> list[CallResult]:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
    )
    sem = asyncio.Semaphore(concurrent)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=5.0)

            async def guarded_call() -> CallResult:
                async with sem:
                    return await _one_call(session, tool, tool_args)

            return await asyncio.gather(*(guarded_call() for _ in range(total)))


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _render(results: list[CallResult], wall_s: float, concurrent: int, total: int, tool: str) -> None:
    oks = [r for r in results if r.ok]
    errs = [r for r in results if not r.ok]
    latencies_ms = sorted(r.latency_s * 1000 for r in oks)

    print()
    print("=" * 60)
    print(f"tool={tool}  concurrent={concurrent}  total={total}")
    print("-" * 60)
    print(f"wall time       {wall_s:.2f}s")
    print(f"throughput      {total / wall_s:,.1f} calls/s")
    print(f"success         {len(oks)}/{total}")
    print(f"errors          {len(errs)}")
    if latencies_ms:
        print(f"latency p50     {_percentile(latencies_ms, 0.50):.2f} ms")
        print(f"latency p95     {_percentile(latencies_ms, 0.95):.2f} ms")
        print(f"latency p99     {_percentile(latencies_ms, 0.99):.2f} ms")
        print(f"latency max     {max(latencies_ms):.2f} ms")
        print(f"latency mean    {statistics.fmean(latencies_ms):.2f} ms")
    if errs:
        print("-" * 60)
        print("first 5 errors:")
        for r in errs[:5]:
            print(f"  {r.latency_s * 1000:6.1f}ms  {r.detail}")
    print("=" * 60)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", default="list_catalogs")
    ap.add_argument("--concurrent", type=int, default=10)
    ap.add_argument("--total", type=int, default=1000)
    ap.add_argument("--args-json", default="{}", help="JSON object passed as tool arguments")
    ap.add_argument("--json", action="store_true", help="Also emit raw results as JSON")
    ns = ap.parse_args()

    tool_args = json.loads(ns.args_json)

    print(f"→ {ns.total} calls to {ns.tool}, {ns.concurrent} in flight", file=sys.stderr)
    t0 = time.monotonic()
    results = await run_load(ns.tool, tool_args, ns.concurrent, ns.total)
    wall = time.monotonic() - t0

    _render(results, wall, ns.concurrent, ns.total, ns.tool)

    if ns.json:
        print()
        print(json.dumps([r.__dict__ for r in results]))


if __name__ == "__main__":
    asyncio.run(main())
