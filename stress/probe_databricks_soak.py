"""Probe: connection / fd / memory soak against the live workspace.

Runs N=1000 list_catalogs calls at a moderate pace (default ~2/sec)
and samples RSS + open-fd count every 50 calls. The point is to catch
two specific failure modes that wouldn't surface in unit tests:

  * the SDK opening a fresh httpx connection per call without reusing
    the pool, monotonically growing fd count
  * tool-task or response-buffer memory accumulating per call

Pass criteria (intentionally generous to absorb GC + httpx pool growth):
  * fd growth from first to last sample ≤ 30
  * RSS growth from first to last sample ≤ 30 MB

If either fails, the diff probably points at a real leak. Tighten the
thresholds once the integration matures.

Run:   python -m stress.probe_databricks_soak [N]
       python -m stress.probe_databricks_soak 200    # quick run
"""

from __future__ import annotations

import asyncio
import gc
import os
import resource
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from mcp_server.databricks.client import get_workspace, run_in_thread


N_DEFAULT = 1000
RATE_S_DEFAULT = 0.5  # min seconds between calls

PASS_FD_GROWTH = 30
PASS_RSS_GROWTH_MB = 30


def _open_fd_count() -> int:
    pid_fd_dir = Path(f"/dev/fd")
    try:
        return len([p for p in pid_fd_dir.iterdir()])
    except OSError:
        return -1


def _rss_mb() -> float:
    # ru_maxrss on macOS is in bytes, on Linux in KB.
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss / (1024 * 1024)
    return rss / 1024  # KB → MB


async def _one_call() -> int:
    """Make one list_catalogs call, return the catalog count."""
    catalogs = await run_in_thread(lambda: list(get_workspace().catalogs.list()))
    return len(catalogs)


async def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else N_DEFAULT
    rate_s = float(sys.argv[2]) if len(sys.argv) > 2 else RATE_S_DEFAULT
    sample_every = max(1, n // 20)

    print(f"[setup] N={n}, rate={rate_s}s/call, sampling every {sample_every} calls")
    print(f"[setup] workspace={os.environ.get('DATABRICKS_HOST', '<not set>')}")
    print()

    # Warm-up: first call constructs the singleton + httpx pool.
    catalog_count = await _one_call()
    print(f"[warmup] saw {catalog_count} catalog(s)")

    samples: list[tuple[int, int, float]] = []  # (call_no, fd_count, rss_mb)
    samples.append((0, _open_fd_count(), _rss_mb()))
    print(f"  call={0:5d}  fd={samples[-1][1]:4d}  rss={samples[-1][2]:6.1f} MB  (baseline)")

    t_start = time.monotonic()
    fail_count = 0
    for i in range(1, n + 1):
        t0 = time.monotonic()
        try:
            await _one_call()
        except Exception as e:
            fail_count += 1
            if fail_count <= 3:
                print(f"  call={i}: error: {type(e).__name__}: {str(e)[:80]}")
        # rate-limit
        elapsed = time.monotonic() - t0
        if elapsed < rate_s:
            await asyncio.sleep(rate_s - elapsed)

        if i % sample_every == 0:
            gc.collect()
            samples.append((i, _open_fd_count(), _rss_mb()))
            s = samples[-1]
            print(f"  call={s[0]:5d}  fd={s[1]:4d}  rss={s[2]:6.1f} MB")

    wall = time.monotonic() - t_start
    print()
    print(f"[done] {n} calls in {wall:.1f}s ({n/wall:.1f}/s), failures={fail_count}")

    fd_first, fd_last = samples[0][1], samples[-1][1]
    rss_first, rss_last = samples[0][2], samples[-1][2]
    fd_growth = fd_last - fd_first
    rss_growth = rss_last - rss_first

    print()
    print(f"  fd:  baseline={fd_first}  end={fd_last}  growth={fd_growth:+d}  (cap +{PASS_FD_GROWTH})")
    print(f"  rss: baseline={rss_first:.1f} MB  end={rss_last:.1f} MB  growth={rss_growth:+.1f} MB  (cap +{PASS_RSS_GROWTH_MB})")

    fd_ok = fd_growth <= PASS_FD_GROWTH
    rss_ok = rss_growth <= PASS_RSS_GROWTH_MB
    fail_ok = fail_count == 0

    print()
    if fd_ok and rss_ok and fail_ok:
        print("PASS")
        return 0
    print("FAIL —", end=" ")
    reasons = []
    if not fd_ok: reasons.append(f"fd grew {fd_growth} > {PASS_FD_GROWTH}")
    if not rss_ok: reasons.append(f"rss grew {rss_growth:.1f} MB > {PASS_RSS_GROWTH_MB}")
    if not fail_ok: reasons.append(f"{fail_count} call failures")
    print("; ".join(reasons))
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
