"""Tests for the system-table convenience tools.

Each tool is a thin wrapper over execute_sql_safe — we patch it to verify
that:
  * args are validated and clamped
  * the constructed SQL contains the expected predicates and limits
  * the response is reshaped from {columns, rows} into named dicts
  * passthrough errors from execute_sql_safe are not modified
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_server.tools import sql_tools


@pytest.fixture
def mock_execute_sql_safe(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(sql_tools, "execute_sql_safe", mock)
    return mock


def _ok_payload(rows, cols):
    return {
        "statement_id": "stmt-x",
        "warehouse_id": "wh-x",
        "columns": [{"name": n, "type": "STRING"} for n in cols],
        "rows": rows,
        "row_count": len(rows),
        "row_limit_applied": 100,
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_list_system_tables_reshapes_rows(mock_execute_sql_safe):
    mock_execute_sql_safe.return_value = _ok_payload(
        rows=[
            ["access", "audit", "MANAGED", "audit log"],
            ["billing", "usage", "MANAGED", "DBU usage"],
        ],
        cols=["table_schema", "table_name", "table_type", "comment"],
    )
    r = await sql_tools.list_system_tables()
    assert r["row_count"] == 2
    assert r["tables"][0] == {
        "table_schema": "access",
        "table_name": "audit",
        "table_type": "MANAGED",
        "comment": "audit log",
    }


@pytest.mark.asyncio
async def test_recent_audit_events_clamps_args(mock_execute_sql_safe):
    mock_execute_sql_safe.return_value = _ok_payload(rows=[], cols=["event_time"])

    # Caller asks for 999 hours / 9999 rows — both well past the cap
    await sql_tools.recent_audit_events(since_hours=999, limit=9999)

    sql_arg = mock_execute_sql_safe.call_args.args[0]
    # 168h is the documented ceiling; 200 rows is the documented ceiling
    assert "INTERVAL 168 HOURS" in sql_arg
    assert "LIMIT 200" in sql_arg
    # And the SDK row_limit kwarg should match, not the user's 9999
    assert mock_execute_sql_safe.call_args.kwargs["row_limit"] == 200


@pytest.mark.asyncio
async def test_recent_audit_events_passes_long_wait_timeout(mock_execute_sql_safe):
    """audit table is known-slow on small warehouses; the tool should
    request the maximum wait (50 s, Databricks ceiling)."""
    mock_execute_sql_safe.return_value = _ok_payload(rows=[], cols=["event_time"])
    await sql_tools.recent_audit_events()
    assert mock_execute_sql_safe.call_args.kwargs.get("wait_timeout_s") == 50


@pytest.mark.asyncio
async def test_recent_query_history_returns_named_dicts(mock_execute_sql_safe):
    mock_execute_sql_safe.return_value = _ok_payload(
        rows=[
            ["2026-04-27T10:00:00Z", "alice@x.com", "FINISHED", "SELECT", "245", "55", "SELECT 1", None],
        ],
        cols=[
            "start_time", "executed_by", "execution_status", "statement_type",
            "total_duration_ms", "produced_rows", "statement_text", "error_message",
        ],
    )
    r = await sql_tools.recent_query_history(since_hours=2, limit=10)
    assert r["since_hours"] == 2
    assert r["row_count"] == 1
    assert r["queries"][0]["executed_by"] == "alice@x.com"
    assert r["queries"][0]["statement_type"] == "SELECT"


@pytest.mark.asyncio
async def test_billing_summary_clamps_to_90_days(mock_execute_sql_safe):
    mock_execute_sql_safe.return_value = _ok_payload(rows=[], cols=["sku_name"])
    await sql_tools.billing_summary(since_days=10000)
    sql_arg = mock_execute_sql_safe.call_args.args[0]
    assert "INTERVAL 90 DAYS" in sql_arg


@pytest.mark.asyncio
async def test_system_tools_passthrough_error(mock_execute_sql_safe):
    """If execute_sql_safe returns an error dict, the wrapper passes it
    through unchanged — does not paper over it."""
    mock_execute_sql_safe.return_value = {
        "error": {"type": "PermissionDenied", "message": "no SELECT on system.access.audit"}
    }
    r = await sql_tools.recent_audit_events()
    assert r == {
        "error": {"type": "PermissionDenied", "message": "no SELECT on system.access.audit"}
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fn, args",
    [
        (sql_tools.recent_audit_events, {"since_hours": "not-an-int"}),
        (sql_tools.recent_query_history, {"limit": "abc"}),
        (sql_tools.billing_summary, {"since_days": "ten"}),
    ],
)
async def test_system_tools_reject_non_int_args(mock_execute_sql_safe, fn, args):
    """Non-int args get caught by _coerce_int; the guard turns the
    ValueError into a structured error before any SQL is constructed."""
    r = await fn(**args)
    assert "error" in r
    # _coerce_int raises ValueError; _guard wraps as ValueError type
    assert r["error"]["type"] == "ValueError"
    mock_execute_sql_safe.assert_not_called()
