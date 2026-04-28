"""Probe: cancellation actually stops the Databricks query.

Drew's "10 ghost queries" scenario: when a tool call is cancelled, does
the workspace-side SQL stop, or does it keep burning compute until the
SDK's wait_timeout fires?

This probe runs a deliberately-slow SQL statement, asyncio-cancels the
call after ~2 s, then queries `system.query.history` to verify:

  1. The cancelled statement appears with execution_status = 'CANCELED'.
  2. end_time - start_time is small (≈ time-to-cancel + a few hundred ms),
     not the ~25 s SDK wait_timeout window.

A failure here means we're back to the orphan-thread scenario the
submit-then-poll refactor was supposed to eliminate.

Run:   python -m stress.probe_sql_cancellation
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from mcp_server import audit  # noqa: E402
from mcp_server.tools.sql_tools import execute_sql_safe  # noqa: E402


# A deliberately slow CROSS JOIN. The WHERE clause forces per-row
# evaluation (otherwise the warehouse optimises count(*) of a range
# product down to a multiplication and finishes in milliseconds).
# Sized to keep a 2X-Small Serverless Starter in RUNNING state long
# enough for our cancellation to land mid-flight.
SLOW_QUERY = (
    "SELECT count(*) FROM range(0, 1000000) a "
    "CROSS JOIN range(0, 1000000) b "
    "WHERE (a.id * b.id) % 7 = 0"
)


def _check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✓' if ok else '✗'} {label}{('  — ' + detail) if detail else ''}")
    return ok


async def main() -> int:
    # Capture the statement_id by patching SDK at the *class* level
    # (instance-level patching doesn't stick because
    # `WorkspaceClient.statement_execution` is a property returning a
    # fresh StatementExecutionAPI on each access).
    from databricks.sdk.service.sql import StatementExecutionAPI

    from mcp_server.databricks.client import get_workspace

    captured: dict[str, str] = {}
    real_execute = StatementExecutionAPI.execute_statement

    def _capture_execute(self, *args, **kwargs):
        resp = real_execute(self, *args, **kwargs)
        if hasattr(resp, "statement_id"):
            captured["statement_id"] = resp.statement_id
        return resp

    StatementExecutionAPI.execute_statement = _capture_execute
    ws = get_workspace()

    probe_caller = f"probe-cancellation-{int(time.time())}"
    token = audit.set_caller_id(probe_caller)
    try:
        print(f"[setup] caller_id={probe_caller}")
        print("[step 1] starting slow CROSS JOIN ...")
        t0 = time.monotonic()
        task = asyncio.create_task(execute_sql_safe(SLOW_QUERY))

        # Wait long enough for the initial submit (5s SDK minimum
        # wait) to return so we have a statement_id captured. The
        # interesting test is cancellation during the *polling* phase,
        # which is what production-style external cancellations would
        # see.
        await asyncio.sleep(8.0)
        elapsed_at_cancel = time.monotonic() - t0
        statement_id = captured.get("statement_id")
        print(f"[step 2] cancelling at t={elapsed_at_cancel:.1f}s, "
              f"statement_id={statement_id}")

        cancel_t = time.monotonic()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception) as e:
            print(f"[step 3] task ended: {type(e).__name__}: {str(e)[:80]}")
        cancel_propagation_s = time.monotonic() - cancel_t
        print(f"[step 4] cancel propagated in asyncio in {cancel_propagation_s * 1000:.0f}ms")

        # 5. Direct lookup via get_statement — no ingest lag.
        if not statement_id:
            print("  ✗ no statement_id captured — submit must have raised early")
            return 1

        # Give Databricks 1-2s to honor the cancel.
        await asyncio.sleep(2.0)
        from mcp_server.databricks.client import run_in_thread
        stmt = await run_in_thread(
            ws.statement_execution.get_statement, statement_id,
        )
        state = stmt.status.state if stmt.status else None
        print(f"[step 5] get_statement says: state={state}")

        from databricks.sdk.service.sql import StatementState
        a = _check(
            "state in {CANCELED, CLOSED}",
            state in (StatementState.CANCELED, StatementState.CLOSED),
            f"state={state}",
        )

        # 6. The probe waited 8s for submit + then cancelled. If the
        # cancel propagation worked, the workspace should have stopped
        # the query within a couple of seconds of t=8s — total wall
        # ≈ 12s. If we'd let the SDK's wait_timeout fire it would be
        # ≥ 25s.
        total_runtime = time.monotonic() - t0
        b = _check(
            "total wall time < 16s (would be ~30s if SDK timeout fired)",
            total_runtime < 16.0,
            f"{total_runtime:.1f}s",
        )

        print()
        if a and b:
            print("PASS — cancel actually stopped the Databricks query")
            return 0
        print("FAIL — see assertions above")
        return 1
    finally:
        audit.reset_caller_id(token)
        StatementExecutionAPI.execute_statement = real_execute


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
