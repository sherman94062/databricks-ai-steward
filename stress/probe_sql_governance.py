"""Probe: SQL governance is the gate Databricks never sees.

For each forbidden statement class, call execute_sql_safe and verify:
  * the response is a SqlNotAllowed structured error (not a workspace
    error, not a crash)
  * the workspace was never contacted — proven by stubbing the
    statement_execution.execute_statement method to a tripwire that
    flips a flag if it's ever called

Pairs with test_sql_safety / test_sql_tools (which test the gate in
isolation). This probe wires the *whole* tool together and confirms
the gate fires before any SDK call.

Run:   python -m stress.probe_sql_governance
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

from mcp_server.databricks import client as db_client
from mcp_server.tools.sql_tools import execute_sql_safe


FORBIDDEN_STATEMENTS = [
    ("INSERT", "INSERT INTO t VALUES (1)"),
    ("UPDATE", "UPDATE t SET x=1"),
    ("DELETE", "DELETE FROM t"),
    ("DROP", "DROP TABLE samples.nyctaxi.trips"),
    ("CREATE", "CREATE TABLE z (x INT)"),
    ("ALTER", "ALTER TABLE t ADD COLUMN y INT"),
    ("MERGE", "MERGE INTO t USING s ON t.id = s.id "
              "WHEN MATCHED THEN UPDATE SET t.x = s.x"),
    ("TRUNCATETABLE", "TRUNCATE TABLE t"),
    ("MULTI_STATEMENT", "SELECT 1; SELECT 2"),
    ("INSERT (CTE-with-DML)", "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x"),
    ("USE", "USE catalog main"),
    ("GRANT", "GRANT SELECT ON t TO `user@x.com`"),
    ("REVOKE", "REVOKE SELECT ON t FROM `user@x.com`"),
]


async def main() -> int:
    # Tripwire: if the gate is bypassed, executing any of these would
    # actually call the SDK. We replace the workspace with a mock that
    # raises if execute_statement is called.
    tripwire = MagicMock()
    tripwire.statement_execution.execute_statement.side_effect = AssertionError(
        "GATE BYPASS: workspace.execute_statement was reached for a forbidden statement"
    )
    db_client.set_workspace_for_tests(tripwire)

    fails: list[str] = []
    try:
        for label, sql in FORBIDDEN_STATEMENTS:
            r = await execute_sql_safe(sql)
            ok = (
                isinstance(r, dict)
                and "error" in r
                and r["error"].get("type") == "SqlNotAllowed"
            )
            mark = "✓" if ok else "✗"
            detail = r.get("error", {}).get("kind") if ok else f"unexpected response: {r}"
            print(f"  {mark} {label:<24}  rejected as {detail}")
            if not ok:
                fails.append(label)

        # Tripwire confirmation: the SDK mock should have zero calls.
        execute_called = tripwire.statement_execution.execute_statement.call_count
        print()
        print(f"workspace.execute_statement call_count = {execute_called}")
        if execute_called != 0:
            fails.append("tripwire-fired")
    finally:
        db_client.set_workspace_for_tests(None)

    print()
    if not fails:
        print("PASS — every forbidden statement rejected before reaching workspace")
        return 0
    print(f"FAIL — {len(fails)} case(s) bypassed the gate: {fails}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
