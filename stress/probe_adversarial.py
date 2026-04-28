"""Adversarial probe — attack-shaped inputs that an LLM acting on
prompt injection might generate. The point isn't to enumerate every
possible attack; it's to *prove* that the existing defenses catch the
classes we care about, so a CISO has empirical evidence rather than
just code review.

Each test case asserts one of:
  * the input is rejected at the governance gate (no workspace contact)
  * the input lands as a structured error (no crash, no leak)
  * the response is bounded (size cap, row cap, scrub) even on weird inputs

We use a tripwire mock for the SDK to confirm "no workspace contact"
where applicable, and the real workspace for the cases that need
end-to-end behavior.

Run:   python -m stress.probe_adversarial
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

from mcp_server import audit, rate_limit
from mcp_server.databricks import client as db_client
from mcp_server.tools.sql_tools import execute_sql_safe


# ---- attack vectors --------------------------------------------------------
# Each entry: (label, sql, expected: "reject" | "execute" — `execute`
# means we expect sqlglot to allow it through; the workspace itself
# would still enforce its own grants).

SQL_ATTACKS: list[tuple[str, str, str]] = [
    # 1. Multi-statement separators — should be rejected as MULTI_STATEMENT
    ("plain semicolon",          "SELECT 1; DROP TABLE t",                    "reject"),
    ("trailing semicolon then DML",
                                  "SELECT 1; INSERT INTO t VALUES (1)",       "reject"),
    ("multiple SELECTs",         "SELECT 1; SELECT 2",                        "reject"),

    # 2. SQL-comment hiding — sqlglot should parse comments correctly and
    #    reject DML inside them. Trick: a `--` line-comment with DML on
    #    the same line. After parse, only the SELECT remains, so this
    #    should ALLOW.
    ("line comment with DML after",
                                  "SELECT 1 -- DROP TABLE t",                 "execute"),
    ("block comment around DML",
                                  "SELECT 1 /* DROP TABLE t */",              "execute"),
    # But a real DML statement after the comment should still be rejected
    # if the parser sees both.
    ("comment then real DML",     "/* note */ DROP TABLE t",                  "reject"),

    # 3. DML hidden inside CTE bodies — covered in test_sql_safety,
    #    re-testing here against the live execute_sql_safe path.
    ("INSERT in CTE",
        "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",                 "reject"),
    ("UPDATE in CTE",
        "WITH x AS (SELECT 1) UPDATE t SET v=1 WHERE id IN (SELECT * FROM x)","reject"),
    ("DELETE in CTE",
        "WITH x AS (SELECT 1) DELETE FROM t WHERE id IN (SELECT * FROM x)",   "reject"),
    ("MERGE in CTE",
        "WITH x AS (SELECT 1) MERGE INTO t USING x ON t.id = x.id "
        "WHEN MATCHED THEN UPDATE SET t.v = x.v",                             "reject"),

    # 4. Whitespace + case tricks — must not bypass keyword detection
    ("UPPERCASE DROP",            "DROP TABLE t",                             "reject"),
    ("lowercase drop",            "drop table t",                             "reject"),
    ("MixEdCase Drop",            "DrOp TaBlE t",                             "reject"),
    ("tabs + newlines",           "DROP\n\tTABLE\n\tt",                       "reject"),
    ("leading whitespace",        "    \n\tDROP TABLE t",                     "reject"),

    # 5. Unicode lookalikes — these are NOT actual DROP, they look like it
    #    in some fonts. sqlglot should treat them as identifiers, not
    #    keywords, so the parse falls through to "unsupported" — reject.
    ("unicode-D in DROP",
        "DROP TABLE t",                                                  "reject"),
    # Cyrillic 'Р' (U+0420) instead of Latin 'P' — visually similar
    ("cyrillic-P in DROP",
        "DROР TABLE t",                                                  "reject"),

    # 6. SET / GRANT / REVOKE — root-level reject via _statement_kind
    ("SET parameter",             "SET spark.foo = 1",                        "reject"),
    ("GRANT SELECT",              "GRANT SELECT ON t TO `x`",                 "reject"),
    ("USE CATALOG",               "USE CATALOG main",                         "reject"),

    # 7. Empty / blank
    ("empty string",              "",                                         "reject"),
    ("whitespace only",           "   \n\t  ",                                "reject"),

    # 8. Garbage — sqlglot may parse as something or fail. We don't
    #    over-promise here: anything that *parses* as a Command keyword
    #    in ALLOWED_KINDS is allowed through. Databricks itself enforces
    #    that the body is well-formed; we don't second-guess.
    ("free text starting with explain",
                                  "explain what this database does",          "execute"),
    ("not really SQL",            "this is not SQL at all",                   "reject"),

    # 9. Massive payloads — should not crash. row_limit will cap any
    #    successful run; here we want the parse step itself to be safe.
    ("very long SELECT",
        "SELECT 1, " + ", ".join(str(i) for i in range(2, 1500)),             "execute"),

    # 10. Allowed-but-tricky valid SELECTs (control group: must allow)
    ("allowed SELECT",            "SELECT 1",                                 "execute"),
    ("allowed CTE",
        "WITH x AS (SELECT 1) SELECT * FROM x",                               "execute"),
    ("allowed UNION",             "SELECT 1 UNION ALL SELECT 2",              "execute"),
    ("allowed EXPLAIN",           "EXPLAIN SELECT 1",                         "execute"),
    ("allowed SHOW",              "SHOW TABLES",                              "execute"),
    ("allowed DESCRIBE",          "DESCRIBE table_name",                      "execute"),
]


def _check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✓' if ok else '✗'} {label:<45}{('  — ' + detail) if detail else ''}")
    return ok


async def test_sql_attacks() -> bool:
    print("[1/3] SQL-shaped adversarial inputs (governance gate must catch the rejects)")
    print(f"      {len(SQL_ATTACKS)} cases\n")

    # Tripwire: if any forbidden case slips past the gate to the SDK,
    # this raises and the probe fails loudly.
    tripwire = MagicMock()
    tripwire.statement_execution.execute_statement.side_effect = AssertionError(
        "GATE BYPASS: workspace.execute_statement was reached for a "
        "supposedly-rejected statement"
    )
    db_client.set_workspace_for_tests(tripwire)
    token = audit.set_caller_id("probe-adversarial-sql")

    fails: list[str] = []
    try:
        for label, sql, expected in SQL_ATTACKS:
            rate_limit.reset_for_tests()
            r = await execute_sql_safe(sql)
            err_type = r.get("error", {}).get("type") if isinstance(r, dict) else None

            if expected == "reject":
                # The governance gate must produce SqlNotAllowed.
                # Anything else is either a missed reject or the gate
                # accepted something it shouldn't.
                ok = err_type == "SqlNotAllowed"
                kind = r.get("error", {}).get("kind") if ok else err_type
                detail = (
                    f"rejected as {kind}" if ok
                    else f"NOT rejected by gate (got {err_type})"
                )
            else:  # "execute"
                # The gate must NOT have produced SqlNotAllowed. Any
                # other outcome — workspace-side error, success — is
                # fine; we're only asserting the gate's allow decision.
                ok = err_type != "SqlNotAllowed"
                detail = (
                    "gate allowed (post-gate behavior is workspace-dependent)"
                    if ok else f"gate REJECTED a known-good input: {r['error']}"
                )

            if not _check(label, ok, detail):
                fails.append(label)

        execute_count = sum(1 for _, _, exp in SQL_ATTACKS if exp == "execute")
        reject_count = len(SQL_ATTACKS) - execute_count
        print()
        print(f"  reject cases:  {reject_count} (governance gate must catch each)")
        print(f"  execute cases: {execute_count} (gate must allow through)")
    finally:
        audit.reset_caller_id(token)
        db_client.set_workspace_for_tests(None)

    return not fails


async def test_argument_attacks() -> bool:
    """Tool args at the MCP boundary — these come from JSON over stdio
    or HTTP, so the JSON parser already handles encoding. We test the
    Python-level args validation that's specific to our tools."""
    print("\n[2/3] Tool-argument attacks")
    fails: list[str] = []

    from mcp_server.tools.sql_tools import (
        billing_summary,
        recent_audit_events,
        recent_query_history,
    )

    # Each system-table tool's int args are clamped via _coerce_int.
    # These should land as ValueError → structured error, never crash.
    cases = [
        ("recent_audit_events(since_hours='abc')",
         lambda: recent_audit_events(since_hours="abc")),
        ("recent_audit_events(since_hours=None)",
         lambda: recent_audit_events(since_hours=None)),
        ("recent_audit_events(since_hours=2**40)",
         lambda: recent_audit_events(since_hours=2 ** 40)),
        ("recent_query_history(limit=-1)",
         lambda: recent_query_history(limit=-1)),
        ("billing_summary(since_days=999999)",
         lambda: billing_summary(since_days=999999)),
        # Boolean / non-int types
        ("recent_audit_events(limit=True)",
         lambda: recent_audit_events(limit=True)),
        # Clamping behaviour: ridiculous limit clamps to 200
        ("recent_audit_events(limit=10**9)",
         lambda: recent_audit_events(limit=10 ** 9)),
    ]

    # Mock the underlying execute_sql_safe so these don't hit the workspace.
    db_client.set_workspace_for_tests(MagicMock())
    token = audit.set_caller_id("probe-adversarial-args")
    try:
        for label, fn in cases:
            rate_limit.reset_for_tests()
            try:
                r = await fn()
                # Pass if structured error or successful clamped call —
                # both are acceptable; we just want no crash.
                ok = isinstance(r, dict)
                detail = f"got {('error' if 'error' in r else 'success') if ok else type(r).__name__}"
            except Exception as e:
                ok = False
                detail = f"raised {type(e).__name__}: {str(e)[:60]}"
            if not _check(label, ok, detail):
                fails.append(label)
    finally:
        audit.reset_caller_id(token)
        db_client.set_workspace_for_tests(None)

    return not fails


async def test_concurrent_rate_limit_race() -> bool:
    """Fire N concurrent calls at a tight per-(tool, caller) rate-limit
    bucket. The bucket is async-locked, so we should see *exactly* the
    cap honoured — not N successes, not 0. Verifies no race that lets
    extra calls slip through under contention."""
    print("\n[3/3] Concurrent rate-limit race (bucket integrity under contention)")

    from mcp_server.app import _guard

    @_guard
    async def my_tool() -> dict:
        return {"ok": True}

    # Set a tight bucket: 3 calls per 60 seconds.
    import os
    old = os.environ.get("MCP_RATE_LIMIT", "")
    os.environ["MCP_RATE_LIMIT"] = "my_tool=3/60"
    rate_limit._OVERRIDES.clear()
    rate_limit._OVERRIDES.update(rate_limit._parse_overrides("my_tool=3/60"))
    rate_limit.reset_for_tests()

    token = audit.set_caller_id("probe-rate-race")
    try:
        results = await asyncio.gather(*(my_tool() for _ in range(20)))
        successes = sum(1 for r in results if r.get("ok"))
        rejected = sum(
            1 for r in results
            if isinstance(r, dict) and "error" in r
            and r["error"].get("type") == "RateLimitExceeded"
        )
        ok = successes == 3 and rejected == 17
        print(f"  out of 20 concurrent calls: {successes} succeeded, "
              f"{rejected} rate-limited")
        if not _check("bucket honoured exactly", ok,
                      f"expected 3 successes, 17 rejections"):
            return False
    finally:
        audit.reset_caller_id(token)
        if old:
            os.environ["MCP_RATE_LIMIT"] = old
        else:
            os.environ.pop("MCP_RATE_LIMIT", None)
        rate_limit._OVERRIDES.clear()
        rate_limit.reset_for_tests()

    return True


async def main() -> int:
    a = await test_sql_attacks()
    b = await test_argument_attacks()
    c = await test_concurrent_rate_limit_race()

    print()
    if a and b and c:
        print("PASS — every attack class blocked at the right defense layer")
        return 0
    print("FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
