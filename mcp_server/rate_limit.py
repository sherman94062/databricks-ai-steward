"""Per-(tool, caller) sliding-window rate limiter.

Defaults are deliberately tight enough that a runaway agent can't burn
through a workspace's API quota or SQL-warehouse cost in seconds:

  * `execute_sql_safe`         5 calls / minute  (cost-bearing)
  * `recent_audit_events`     10 calls / minute  (warehouse-bound)
  * `recent_query_history`    10 calls / minute  (warehouse-bound)
  * `billing_summary`         10 calls / minute  (warehouse-bound)
  * everything else           50 calls / minute  (cheap metadata)

Override via `MCP_RATE_LIMIT` environment variable, e.g.:
  MCP_RATE_LIMIT="execute_sql_safe=10/60,*=200/60"

The limiter is in-memory and per-process — appropriate for a single
MCP server subprocess. A multi-replica deployment would replace this
with a shared backend (Redis), but the tool-side surface (`check`)
stays the same.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class _Limit:
    count: int          # max calls
    window_s: int       # within this many seconds


_DEFAULT_LIMITS: dict[str, _Limit] = {
    "execute_sql_safe":     _Limit(5,  60),
    "recent_audit_events":  _Limit(10, 60),
    "recent_query_history": _Limit(10, 60),
    "billing_summary":      _Limit(10, 60),
    "list_system_tables":   _Limit(10, 60),
}
_FALLBACK = _Limit(50, 60)


def _parse_overrides(spec: str) -> dict[str, _Limit]:
    """Parse `MCP_RATE_LIMIT="tool=N/W,*=N/W"` into a dict."""
    out: dict[str, _Limit] = {}
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, rhs = chunk.split("=", 1)
        try:
            count, window = rhs.split("/", 1)
            out[key.strip()] = _Limit(int(count), int(window))
        except (ValueError, AttributeError):
            continue
    return out


_OVERRIDES = _parse_overrides(os.environ.get("MCP_RATE_LIMIT", ""))


def _limit_for(tool: str) -> _Limit:
    if tool in _OVERRIDES:
        return _OVERRIDES[tool]
    if "*" in _OVERRIDES:
        return _OVERRIDES["*"]
    return _DEFAULT_LIMITS.get(tool, _FALLBACK)


_buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_lock = asyncio.Lock()


class RateLimitExceeded(Exception):
    def __init__(self, tool: str, caller: str, limit: _Limit):
        self.tool = tool
        self.caller = caller
        self.limit = limit
        super().__init__(
            f"rate limit exceeded for {tool!r} (caller {caller!r}): "
            f"{limit.count} calls per {limit.window_s} seconds"
        )


async def check(tool: str, caller: str) -> _Limit:
    """Record one call against (tool, caller). Raises `RateLimitExceeded`
    if the bucket is full. Returns the active limit on success (so the
    caller can include it in audit metadata)."""
    limit = _limit_for(tool)
    key = (tool, caller)
    now = time.monotonic()
    async with _lock:
        bucket = _buckets[key]
        cutoff = now - limit.window_s
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit.count:
            raise RateLimitExceeded(tool, caller, limit)
        bucket.append(now)
    return limit


def reset_for_tests() -> None:
    """Clear all buckets. Tests call this between cases."""
    _buckets.clear()
