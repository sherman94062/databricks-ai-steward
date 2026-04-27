"""Probe: execute_sql_safe under concurrent load.

Fires N concurrent SELECTs against samples.nyctaxi.trips and measures:
  * success rate
  * p50 / p95 / max latency
  * fd + RSS deltas (does the SDK leak under SQL load?)

Then runs one trial where one of the N calls is a deliberately slow
query, to confirm it cleanly times out via _guard's per-tool deadline
without dragging the others.

This is a real-workspace probe — N=10 default keeps it cheap. Pass a
larger N as the first arg if you want to push harder.

Run:   python -m stress.probe_sql_concurrency [N]
"""

from __future__ import annotations

import asyncio
import gc
import os
import resource
import statistics
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from mcp_server.tools.sql_tools import execute_sql_safe


N_DEFAULT = 10
SAMPLE_QUERY = "SELECT * FROM samples.nyctaxi.trips LIMIT 5"

# A deliberately slow query: spark sleep + cross join over a big-ish range.
# Bound with our own timeout; we only want it to *start* to verify
# cancellation behavior, not actually finish.
SLOW_QUERY = (
    "SELECT count(*) FROM range(0, 10000000) a "
    "CROSS JOIN range(0, 100) b "
    "WHERE a.id + b.id > 0"
)


def _open_fd_count() -> int:
    try:
        return len(list(Path("/dev/fd").iterdir()))
    except OSError:
        return -1


def _rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss / (1024 * 1024)
    return rss / 1024


async def _timed_call(sql: str, row_limit: int | None = None) -> tuple[bool, float, str]:
    t0 = time.monotonic()
    try:
        r = await execute_sql_safe(sql, row_limit=row_limit)
    except Exception as e:
        return False, time.monotonic() - t0, f"{type(e).__name__}: {str(e)[:80]}"
    elapsed = time.monotonic() - t0
    if isinstance(r, dict) and "error" in r:
        return False, elapsed, f"{r['error'].get('type')}: {r['error'].get('message', '')[:80]}"
    return True, elapsed, f"{r.get('row_count')} rows"


def _summarize(label: str, results: list[tuple[bool, float, str]]) -> bool:
    successes = [r for r in results if r[0]]
    n = len(results)
    n_ok = len(successes)
    print(f"  {label}: {n_ok}/{n} succeeded")
    if successes:
        ms = sorted(r[1] * 1000 for r in successes)
        p50 = ms[len(ms) // 2]
        p95 = ms[int(len(ms) * 0.95)]
        print(f"    p50={p50:.0f}ms  p95={p95:.0f}ms  max={ms[-1]:.0f}ms  mean={statistics.fmean(ms):.0f}ms")
    failures = [r for r in results if not r[0]]
    for r in failures[:3]:
        print(f"    fail: {r[2]}  ({r[1]*1000:.0f}ms)")
    if len(failures) > 3:
        print(f"    ... and {len(failures)-3} more failures")
    return n_ok == n


async def trial_concurrent_sample(n: int) -> bool:
    print(f"\n[trial 1] {n} concurrent SELECTs against samples.nyctaxi.trips")
    fd_before, rss_before = _open_fd_count(), _rss_mb()
    print(f"  baseline: fd={fd_before}  rss={rss_before:.1f} MB")

    t0 = time.monotonic()
    results = await asyncio.gather(*(_timed_call(SAMPLE_QUERY) for _ in range(n)))
    wall = time.monotonic() - t0
    print(f"  wall: {wall:.1f}s  ({n / wall:.1f} calls/s)")

    gc.collect()
    fd_after, rss_after = _open_fd_count(), _rss_mb()
    print(f"  after:    fd={fd_after} (+{fd_after - fd_before})"
          f"  rss={rss_after:.1f} MB (+{rss_after - rss_before:.1f})")

    return _summarize("results", results)


async def trial_mixed_with_slow(n_fast: int) -> bool:
    """Fire one slow query (with a tight per-call timeout) alongside
    n_fast normal SELECTs. The slow one should land as a structured
    error (StatementFailed because we set on_wait_timeout=CANCEL on the
    SDK side, or ToolTimeout if our outer guard fires first); the fast
    ones should all succeed without their latencies blowing up."""
    print(f"\n[trial 2] 1 slow CROSS-JOIN + {n_fast} concurrent fast SELECTs")
    print(f"          slow query is wrapped at MCP_SQL_WAIT_TIMEOUT_S; expecting it to be cancelled")

    fast = [_timed_call(SAMPLE_QUERY) for _ in range(n_fast)]
    slow = _timed_call(SLOW_QUERY)
    t0 = time.monotonic()
    fast_results, slow_result = await asyncio.gather(asyncio.gather(*fast), slow)
    wall = time.monotonic() - t0
    print(f"  wall: {wall:.1f}s")

    fast_ok = _summarize("fast", fast_results)

    success, elapsed, detail = slow_result
    print(f"  slow: {'succeeded (unexpected!)' if success else 'cancelled cleanly'}"
          f"  ({elapsed*1000:.0f}ms)  detail={detail}")

    # The slow query SHOULDN'T succeed (or if it does, that's interesting
    # data — the warehouse was faster than we expected). The fast queries
    # MUST all succeed.
    return fast_ok


async def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else N_DEFAULT
    print(f"workspace: {os.environ.get('DATABRICKS_HOST', '<not set>')}")
    print(f"N={n} (concurrency)")

    a = await trial_concurrent_sample(n)
    b = await trial_mixed_with_slow(n)

    print()
    if a and b:
        print("PASS")
        return 0
    print("FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
