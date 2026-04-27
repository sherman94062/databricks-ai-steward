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
"""

from __future__ import annotations

import os
from typing import Any

from databricks.sdk.service.sql import (
    Disposition,
    Format,
    StatementState,
    ExecuteStatementRequestOnWaitTimeout,
)

from mcp_server.app import safe_tool
from mcp_server.databricks.client import (
    WarehouseUnavailable,
    get_workspace,
    resolve_warehouse_id,
    run_in_thread,
)
from mcp_server.databricks.sql_safety import classify


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


def _execute_blocking(sql: str, warehouse_id: str, row_limit: int):
    """Synchronous SDK call. Wrap with run_in_thread."""
    ws = get_workspace()
    return ws.statement_execution.execute_statement(
        statement=sql,
        warehouse_id=warehouse_id,
        row_limit=row_limit,
        wait_timeout=f"{_WAIT_TIMEOUT_S}s",
        on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CANCEL,
        disposition=Disposition.INLINE,
        format=Format.JSON_ARRAY,
    )


@safe_tool()
async def execute_sql_safe(
    sql: str,
    warehouse_id: str | None = None,
    row_limit: int | None = None,
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
    resp = await run_in_thread(_execute_blocking, sql, wh_id, requested)

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
