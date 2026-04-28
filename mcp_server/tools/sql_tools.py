"""SQL pass-through tool with governance.

`execute_sql_safe` is the single chokepoint for arbitrary SQL against
Databricks. Its contract:

  * read-only via `sql_safety.classify` — SELECT / EXPLAIN / SHOW /
    DESCRIBE only, single statement, no DML/DDL nested inside CTEs
  * row-capped via the SDK's `row_limit` so even a `SELECT *` against
    a billion-row table returns a bounded payload
  * warehouse auto-resolved per the precedence in
    `databricks.client.resolve_warehouse_id`
  * SDK errors land as the structured `_guard` error response, not
    raised exceptions
  * **on cancellation, the Databricks statement itself is cancelled** —
    not just the asyncio task. See `_execute_with_cancellation` for the
    submit-then-poll pattern that makes this possible (the alternative —
    blocking on `execute_statement(wait_timeout=N, on_wait_timeout=CANCEL)`
    — leaves the workspace query running until the SDK's own wait
    expires, even after the asyncio task has been cancelled, burning
    warehouse compute on requests nobody is listening to anymore).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from databricks.sdk.service.sql import (
    Disposition,
    ExecuteStatementRequestOnWaitTimeout,
    Format,
    QueryTag,
    StatementState,
)

from mcp_server.app import safe_tool
from mcp_server.audit import current_caller_id
from mcp_server.databricks.client import (
    WarehouseUnavailable,
    get_workspace,
    resolve_warehouse_id,
    run_in_thread,
)
from mcp_server.databricks.sql_safety import classify

log = logging.getLogger(__name__)
_INFLIGHT_STATES = (StatementState.PENDING, StatementState.RUNNING)

_DEFAULT_ROW_LIMIT = int(os.environ.get("MCP_SQL_ROW_LIMIT", "100"))
_HARD_ROW_LIMIT = int(os.environ.get("MCP_SQL_HARD_ROW_LIMIT", "1000"))

# Wait inline for up to this long; if the query is still running we
# CANCEL it and surface a ToolTimeout-shaped error. Capped at 50s by
# Databricks; defaulted shorter than MCP_TOOL_TIMEOUT_S so the SDK
# round-trip clearly bounds the whole tool call.
_WAIT_TIMEOUT_S = max(5, min(50, int(os.environ.get("MCP_SQL_WAIT_TIMEOUT_S", "25"))))


def _statement_to_payload(resp, requested_limit: int) -> dict:
    manifest = resp.manifest
    columns = []
    if manifest and manifest.schema and manifest.schema.columns:
        columns = [
            {"name": c.name, "type": c.type_text}
            for c in manifest.schema.columns
        ]
    rows: list[list[Any]] = []
    if resp.result and resp.result.data_array:
        rows = resp.result.data_array
    return {
        "statement_id": resp.statement_id,
        "warehouse_id": getattr(resp, "warehouse_id", None),
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "row_limit_applied": requested_limit,
        # Databricks sets manifest.truncated when row_limit cut the result.
        "truncated": bool(manifest and getattr(manifest, "truncated", False)),
    }


async def _cancel_quietly(ws, statement_id: str) -> None:
    """Best-effort `cancel_execution`. Never raises — the caller is
    already on a cancellation path and shouldn't have to worry about
    the cleanup itself failing."""
    try:
        await asyncio.to_thread(
            ws.statement_execution.cancel_execution, statement_id,
        )
        log.warning(
            "cancelled Databricks statement %s on tool cancellation",
            statement_id,
        )
    except Exception:
        log.exception(
            "failed to cancel Databricks statement %s — manual cleanup may be needed",
            statement_id,
        )


async def _execute_with_cancellation(
    sql: str, warehouse_id: str, row_limit: int, wait_timeout_s: int,
):
    """Submit a SQL statement, poll until done, and on any cancellation
    fire `cancel_execution(statement_id)` so Databricks actually stops
    the query.

    Why this matters: the previous synchronous-wait pattern
    (`execute_statement(wait_timeout=25s, on_wait_timeout=CANCEL)`)
    relied on the SDK's own timer to cancel the workspace query.
    Anything that cancelled the asyncio task before that timer fired —
    an external `asyncio.wait_for`, the upstream
    `notifications/cancelled` once it ships, our own outer
    `MCP_TOOL_TIMEOUT_S` — would orphan a worker thread that kept
    polling Databricks until the SDK's window expired (~25 s), still
    consuming warehouse compute on a request nobody was listening to.

    The two-phase submit-then-poll pattern below makes the asyncio
    cancellation point reachable: we get the `statement_id` back from
    the initial submit fast (5s wait, CONTINUE on timeout), then poll
    `get_statement` with `asyncio.sleep` between calls. Sleeps are
    cancellation points; on `CancelledError` (or any other exception)
    we call `cancel_execution(statement_id)` before re-raising.
    """
    ws = get_workspace()
    # query_tags propagates the MCP caller identity (set per-call by the
    # transport layer's audit hook) into Databricks' own audit trail —
    # `system.query.history` and the workspace UI both surface these
    # tags, so a human auditor can attribute statements to the agent
    # that triggered them, even though every statement runs under the
    # same PAT.
    tags = [
        QueryTag(key="mcp_caller", value=current_caller_id()),
        QueryTag(key="mcp_source", value="databricks-ai-steward"),
    ]

    # Phase 1: submit. wait_timeout="5s" is the SDK's minimum allowed
    # value, and on_wait_timeout=CONTINUE means we get a statement_id
    # back fast even if the query hasn't started yet.
    resp = await asyncio.to_thread(
        ws.statement_execution.execute_statement,
        statement=sql,
        warehouse_id=warehouse_id,
        row_limit=row_limit,
        wait_timeout="5s",
        on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CONTINUE,
        disposition=Disposition.INLINE,
        format=Format.JSON_ARRAY,
        query_tags=tags,
    )
    statement_id = resp.statement_id

    # Fast path: statement finished within the 5s inline-wait window.
    state = resp.status.state if resp.status else None
    if state is None or state not in _INFLIGHT_STATES:
        return resp

    # Defensive: every SDK response we've seen returns a non-empty
    # statement_id when state is PENDING/RUNNING. If we somehow get
    # here without one, we can't drive the poll/cancel cycle — log
    # and bail back to the caller with whatever we have.
    if not statement_id:
        log.warning("execute_statement returned no statement_id with state=%s", state)
        return resp

    # Phase 2: poll until done or our budget runs out.
    deadline = asyncio.get_event_loop().time() + max(0, wait_timeout_s - 5)
    poll_interval_s = 0.5

    try:
        while resp.status and resp.status.state in _INFLIGHT_STATES:
            now = asyncio.get_event_loop().time()
            if now >= deadline:
                # Our own budget exhausted — cancel server-side and
                # return the now-CANCELED status so the caller sees a
                # structured StatementFailed error.
                await _cancel_quietly(ws, statement_id)
                return await asyncio.to_thread(
                    ws.statement_execution.get_statement, statement_id,
                )
            await asyncio.sleep(min(poll_interval_s, max(0.05, deadline - now)))
            resp = await asyncio.to_thread(
                ws.statement_execution.get_statement, statement_id,
            )
        return resp
    except BaseException:
        # CancelledError, KeyboardInterrupt, or anything else. Kill the
        # workspace-side statement before letting the exception
        # propagate. _cancel_quietly swallows its own errors so it
        # never masks the original exception.
        await _cancel_quietly(ws, statement_id)
        raise


def _coerce_int(value, name: str, lo: int, hi: int) -> int:
    """Validate an integer arg landing from the MCP wire (where it may
    arrive as int or as a string). Clamp to [lo, hi]."""
    try:
        n = int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name} must be an integer; got {value!r}") from e
    return max(lo, min(hi, n))


def _rows_to_dicts(payload: dict) -> list[dict]:
    """Reshape an execute_sql_safe success payload into [{col_name: value, ...}].

    `strict=False` is intentional: Databricks' INLINE format always
    returns row tuples whose width matches the schema, but on the
    margin (e.g. when a row is malformed and the SDK truncates) we
    prefer to emit a partial dict over raising mid-call. The
    underlying invariant — len(row) == len(cols) — is enforced by
    Databricks, not by us.
    """
    cols = [c["name"] for c in payload.get("columns", [])]
    return [dict(zip(cols, row, strict=False)) for row in payload.get("rows", [])]


@safe_tool(timeout_s=60)
async def execute_sql_safe(
    sql: str,
    warehouse_id: str | None = None,
    row_limit: int | None = None,
    wait_timeout_s: int | None = None,
) -> dict:
    """Run a read-only SQL statement against Databricks SQL.

    Allowed statement kinds: SELECT, EXPLAIN, SHOW, DESCRIBE — anything
    else is rejected before reaching the workspace. Multi-statement
    input (semicolon-separated) and DML/DDL hidden inside CTEs are also
    rejected. Rows are server-capped by `row_limit` (default 100, hard
    ceiling 1000); the response includes `truncated` when the cap
    fired. Warehouse is resolved from the explicit arg, then
    `MCP_DATABRICKS_WAREHOUSE_ID`, then the first running warehouse in
    the workspace.
    """
    # 1. Governance gate — never touches the workspace.
    verdict = classify(sql)
    if not verdict.allowed:
        return {
            "error": {
                "type": "SqlNotAllowed",
                "kind": verdict.kind,
                "message": verdict.reason or "statement not allowed",
            }
        }

    # 2. Cap the row limit. Caller may request less than default but
    # cannot exceed the hard ceiling.
    requested = row_limit if row_limit is not None else _DEFAULT_ROW_LIMIT
    if requested < 1:
        requested = 1
    if requested > _HARD_ROW_LIMIT:
        requested = _HARD_ROW_LIMIT

    # 3. Resolve warehouse.
    try:
        wh_id = await run_in_thread(resolve_warehouse_id, warehouse_id)
    except WarehouseUnavailable as e:
        return {"error": {"type": "WarehouseUnavailable", "message": str(e)}}

    # 4. Execute. SDK errors fall through to _guard.
    wait_s = max(5, min(50, int(wait_timeout_s) if wait_timeout_s is not None else _WAIT_TIMEOUT_S))
    resp = await _execute_with_cancellation(sql, wh_id, requested, wait_s)

    # 5. Map non-success states to a structured error so the caller
    # gets a consistent shape regardless of where the failure happened.
    state = resp.status.state if resp.status else None
    if state != StatementState.SUCCEEDED:
        msg = ""
        if resp.status and resp.status.error:
            msg = resp.status.error.message or ""
        return {
            "error": {
                "type": "StatementFailed",
                "state": state.value if state else "UNKNOWN",
                "statement_id": resp.statement_id,
                "message": msg or f"statement ended in state {state}",
            }
        }

    return _statement_to_payload(resp, requested)


# ---- system-table convenience tools ----------------------------------------
# Each one is a thin wrapper over execute_sql_safe: int args are validated,
# SQL is constructed with safe f-string interpolation (only ints reach SQL),
# and the result is reshaped from {columns, rows} into named dicts so AI
# clients don't have to zip them by hand.
#
# All four respect the same governance gate as execute_sql_safe — they're
# only safe because they call SELECTs the gate already allows.


@safe_tool(timeout_s=60)
async def list_system_tables() -> dict:
    """List tables in the `system` catalog visible to the configured PAT.

    The `system` catalog holds operational metadata (audit, billing, query
    history, compute, lakeflow, mlflow, etc.). Visibility depends on Unity
    Catalog grants — schemas exist as namespaces but the underlying tables
    may not all be readable. This call uses
    `system.information_schema.tables` so it returns exactly what the
    caller can read.
    """
    sql = (
        "SELECT table_schema, table_name, table_type, comment "
        "FROM system.information_schema.tables "
        "WHERE table_catalog = 'system' "
        "ORDER BY table_schema, table_name"
    )
    raw = await execute_sql_safe(sql, row_limit=500)
    if "error" in raw:
        return raw
    return {
        "tables": _rows_to_dicts(raw),
        "row_count": raw["row_count"],
        "truncated": raw["truncated"],
    }


@safe_tool(timeout_s=60)
async def recent_audit_events(
    since_hours: int = 24,
    limit: int = 50,
) -> dict:
    """Recent rows from `system.access.audit`. Workspace-level audit log:
    every action by every principal, the service that handled it, and the
    response status.

    Args:
        since_hours: lookback window. Clamped to [1, 168] (1 hour to 7 days).
        limit:       max rows. Clamped to [1, 200].

    Caveat: on small warehouses (e.g. Free Edition's 2X-Small Serverless
    Starter), this query may exceed Databricks' 50 s synchronous wait
    ceiling and return `StatementFailed: CANCELED`. Workarounds:
    upgrade the warehouse, or call `execute_sql_safe` directly with
    additional predicates (e.g. `service_name = 'unityCatalog'`).
    """
    h = _coerce_int(since_hours, "since_hours", 1, 168)
    n = _coerce_int(limit, "limit", 1, 200)
    # event_date is the table's partition column — pruning by it makes
    # the filter cheap. event_time gives the precise window inside the
    # selected partitions. Without the date filter this query scans
    # everything and times out on high-volume workspaces.
    days = max(1, (h + 23) // 24)
    # nosec B608 — h, n, days are all int-validated and clamped via
    # _coerce_int before they reach this f-string. No user-controlled
    # text can flow into the SQL. The constructed statement also
    # passes the sqlglot governance gate inside execute_sql_safe.
    sql = (
        "SELECT event_time, user_identity.email AS user_email, "
        "service_name, action_name, response.status_code AS status_code, "
        "request_params "
        "FROM system.access.audit "
        f"WHERE event_date >= current_date() - INTERVAL {days} DAYS "  # nosec B608
        f"  AND event_time > current_timestamp() - INTERVAL {h} HOURS "  # nosec B608
        "ORDER BY event_time DESC "
        f"LIMIT {n}"  # nosec B608
    )
    raw = await execute_sql_safe(sql, row_limit=n, wait_timeout_s=50)
    if "error" in raw:
        return raw
    return {
        "events": _rows_to_dicts(raw),
        "since_hours": h,
        "row_count": raw["row_count"],
        "truncated": raw["truncated"],
    }


@safe_tool(timeout_s=60)
async def recent_query_history(
    since_hours: int = 1,
    limit: int = 50,
) -> dict:
    """Recent rows from `system.query.history`. Every SQL warehouse
    statement: who ran it, how long it took, status, and statement text.

    Args:
        since_hours: lookback window. Clamped to [1, 168] (1 hour to 7 days).
        limit:       max rows. Clamped to [1, 200].
    """
    h = _coerce_int(since_hours, "since_hours", 1, 168)
    n = _coerce_int(limit, "limit", 1, 200)
    # nosec B608 — h, n are int-validated by _coerce_int.
    sql = (
        "SELECT start_time, executed_by, execution_status, statement_type, "
        "total_duration_ms, produced_rows, statement_text, error_message "
        "FROM system.query.history "
        f"WHERE start_time > current_timestamp() - INTERVAL {h} HOURS "  # nosec B608
        "ORDER BY start_time DESC "
        f"LIMIT {n}"  # nosec B608
    )
    raw = await execute_sql_safe(sql, row_limit=n)
    if "error" in raw:
        return raw
    return {
        "queries": _rows_to_dicts(raw),
        "since_hours": h,
        "row_count": raw["row_count"],
        "truncated": raw["truncated"],
    }


@safe_tool(timeout_s=60)
async def billing_summary(since_days: int = 7) -> dict:
    """Aggregate DBU consumption from `system.billing.usage`, grouped by
    SKU. Useful for cost questions like "what spent the most last week".

    Args:
        since_days: lookback window in days. Clamped to [1, 90].
    """
    d = _coerce_int(since_days, "since_days", 1, 90)
    # nosec B608 — d is int-validated by _coerce_int.
    sql = (
        "SELECT sku_name, billing_origin_product, "
        "sum(usage_quantity) AS total_units, "
        "count(*) AS record_count, "
        "any_value(usage_unit) AS unit "
        "FROM system.billing.usage "
        f"WHERE usage_date > current_date() - INTERVAL {d} DAYS "  # nosec B608
        "GROUP BY sku_name, billing_origin_product "
        "ORDER BY total_units DESC"
    )
    raw = await execute_sql_safe(sql, row_limit=200)
    if "error" in raw:
        return raw
    return {
        "summary": _rows_to_dicts(raw),
        "since_days": d,
        "row_count": raw["row_count"],
        "truncated": raw["truncated"],
    }
