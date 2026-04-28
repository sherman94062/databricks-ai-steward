"""execute_sql_safe tool tests.

Mocks the WorkspaceClient so tests never touch a live workspace.
Verifies governance + cap + warehouse-resolution + SDK-error handling.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from databricks.sdk.errors import PermissionDenied
from databricks.sdk.service.sql import StatementState

from mcp_server.databricks import client as db_client
from mcp_server.tools import sql_tools


def _success_response(rows, columns):
    """Mimic the bits of databricks.sdk.service.sql.StatementResponse we read."""
    schema_cols = [
        SimpleNamespace(name=name, type_text=type_text)
        for name, type_text in columns
    ]
    return SimpleNamespace(
        statement_id="stmt-test",
        warehouse_id="wh-test",
        status=SimpleNamespace(state=StatementState.SUCCEEDED, error=None),
        manifest=SimpleNamespace(
            schema=SimpleNamespace(columns=schema_cols),
            truncated=len(rows) >= 1000,
        ),
        result=SimpleNamespace(data_array=rows),
    )


@pytest.fixture
def mock_workspace(monkeypatch):
    ws = MagicMock()
    db_client.set_workspace_for_tests(ws)
    # Stub the warehouse resolver to bypass the warehouses.list() round-trip.
    monkeypatch.setattr(
        "mcp_server.tools.sql_tools.resolve_warehouse_id",
        lambda explicit=None: explicit or "wh-fake",
    )
    yield ws
    db_client.set_workspace_for_tests(None)


@pytest.mark.asyncio
async def test_select_returns_rows_and_columns(mock_workspace):
    mock_workspace.statement_execution.execute_statement.return_value = (
        _success_response(
            rows=[["1", "hello"], ["2", "world"]],
            columns=[("x", "INT"), ("y", "STRING")],
        )
    )
    result = await sql_tools.execute_sql_safe("SELECT 1 AS x, 'hello' AS y")
    assert result["row_count"] == 2
    assert result["columns"] == [
        {"name": "x", "type": "INT"},
        {"name": "y", "type": "STRING"},
    ]
    assert result["rows"] == [["1", "hello"], ["2", "world"]]
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_dml_rejected_without_calling_workspace(mock_workspace):
    result = await sql_tools.execute_sql_safe("DROP TABLE x")
    assert result["error"]["type"] == "SqlNotAllowed"
    assert result["error"]["kind"] == "DROP"
    mock_workspace.statement_execution.execute_statement.assert_not_called()


@pytest.mark.asyncio
async def test_multi_statement_rejected(mock_workspace):
    result = await sql_tools.execute_sql_safe("SELECT 1; SELECT 2")
    assert result["error"]["type"] == "SqlNotAllowed"
    assert result["error"]["kind"] == "MULTI_STATEMENT"
    mock_workspace.statement_execution.execute_statement.assert_not_called()


@pytest.mark.asyncio
async def test_row_limit_capped_at_hard_ceiling(mock_workspace):
    """Caller-supplied row_limit above the hard ceiling is silently capped."""
    mock_workspace.statement_execution.execute_statement.return_value = (
        _success_response(rows=[], columns=[("x", "INT")])
    )
    result = await sql_tools.execute_sql_safe(
        "SELECT * FROM t",
        row_limit=999_999,  # well past the ceiling
    )
    # The applied limit shows up in the response and was forwarded to the SDK.
    assert result["row_limit_applied"] == sql_tools._HARD_ROW_LIMIT
    call_kwargs = mock_workspace.statement_execution.execute_statement.call_args.kwargs
    assert call_kwargs["row_limit"] == sql_tools._HARD_ROW_LIMIT


@pytest.mark.asyncio
async def test_failed_statement_becomes_structured_error(mock_workspace):
    failed_resp = SimpleNamespace(
        statement_id="stmt-fail",
        warehouse_id="wh-test",
        status=SimpleNamespace(
            state=StatementState.FAILED,
            error=SimpleNamespace(message="syntax error near 'asdf'"),
        ),
        manifest=None,
        result=None,
    )
    mock_workspace.statement_execution.execute_statement.return_value = failed_resp
    result = await sql_tools.execute_sql_safe("SELECT 1")
    assert result["error"]["type"] == "StatementFailed"
    assert result["error"]["state"] == "FAILED"
    assert "syntax error" in result["error"]["message"]


@pytest.mark.asyncio
async def test_sdk_exception_lands_through_guard(mock_workspace):
    mock_workspace.statement_execution.execute_statement.side_effect = (
        PermissionDenied("user lacks SELECT on samples.nyctaxi.trips")
    )
    result = await sql_tools.execute_sql_safe("SELECT * FROM samples.nyctaxi.trips")
    assert result["error"]["type"] == "PermissionDenied"


@pytest.mark.asyncio
async def test_caller_id_propagates_to_databricks_query_tags(mock_workspace):
    """The MCP caller identity is set per-request via a contextvar and
    must reach Databricks as `query_tags` so the workspace's own audit
    trail can attribute statements to the agent that triggered them."""
    from mcp_server import audit

    mock_workspace.statement_execution.execute_statement.return_value = (
        _success_response(rows=[], columns=[("x", "INT")])
    )
    token = audit.set_caller_id("agent-alpha")
    try:
        await sql_tools.execute_sql_safe("SELECT 1")
    finally:
        audit.reset_caller_id(token)

    call_kwargs = mock_workspace.statement_execution.execute_statement.call_args.kwargs
    tags = call_kwargs.get("query_tags") or []
    by_key = {t.key: t.value for t in tags}
    assert by_key.get("mcp_caller") == "agent-alpha"
    assert by_key.get("mcp_source") == "databricks-ai-steward"


@pytest.mark.asyncio
async def test_cancellation_calls_cancel_execution_at_databricks(mock_workspace):
    """When asyncio cancels execute_sql_safe mid-poll, the workspace-side
    statement must be cancelled too. Without this, a cancelled tool call
    leaves the warehouse running the query until the SDK's wait_timeout
    fires (~25s) — Drew's "10 ghost queries" scenario."""
    from databricks.sdk.service.sql import StatementState

    pending = SimpleNamespace(
        statement_id="stmt-pending-001",
        warehouse_id="wh-fake",
        status=SimpleNamespace(state=StatementState.PENDING, error=None),
        manifest=None,
        result=None,
    )
    running = SimpleNamespace(
        statement_id="stmt-pending-001",
        warehouse_id="wh-fake",
        status=SimpleNamespace(state=StatementState.RUNNING, error=None),
        manifest=None,
        result=None,
    )

    # First call (execute_statement) returns PENDING (still in flight).
    mock_workspace.statement_execution.execute_statement.return_value = pending
    # Subsequent get_statement calls all return RUNNING — the loop
    # never completes naturally, so cancellation is the only exit.
    mock_workspace.statement_execution.get_statement.return_value = running

    task = asyncio.create_task(
        sql_tools.execute_sql_safe("SELECT * FROM samples.nyctaxi.trips")
    )
    # Yield enough times for the poll loop to enter at least one
    # asyncio.sleep — that's where cancellation will land.
    await asyncio.sleep(0.6)
    task.cancel()

    # The cancellation should propagate as ToolTimeout via _guard
    # (CancelledError is caught and reported), OR the task itself
    # may report it as cancelled depending on timing. Either way,
    # cancel_execution must have been called with our statement_id.
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task

    mock_workspace.statement_execution.cancel_execution.assert_called_with(
        "stmt-pending-001"
    )


@pytest.mark.asyncio
async def test_deadline_expiry_cancels_at_databricks(mock_workspace, monkeypatch):
    """When the wait_timeout_s budget expires while the statement is
    still RUNNING, we cancel server-side and return the post-cancel
    state. No ghost query."""
    from databricks.sdk.service.sql import StatementState

    pending = SimpleNamespace(
        statement_id="stmt-deadline-001",
        warehouse_id="wh-fake",
        status=SimpleNamespace(state=StatementState.PENDING, error=None),
        manifest=None,
        result=None,
    )
    canceled = SimpleNamespace(
        statement_id="stmt-deadline-001",
        warehouse_id="wh-fake",
        status=SimpleNamespace(state=StatementState.CANCELED, error=None),
        manifest=None,
        result=None,
    )
    mock_workspace.statement_execution.execute_statement.return_value = pending
    # Always RUNNING during the loop, then return CANCELED after we
    # cancel server-side and re-fetch.
    states = iter([pending, pending, canceled])
    mock_workspace.statement_execution.get_statement.side_effect = (
        lambda *a, **kw: next(states, canceled)
    )

    # wait_timeout_s=6 → after the 5s inline-wait, our poll budget is 1s.
    # The poll loop should hit the deadline quickly.
    result = await sql_tools.execute_sql_safe(
        "SELECT * FROM samples.nyctaxi.trips", wait_timeout_s=6,
    )
    # Caller sees structured error reporting CANCELED.
    assert result["error"]["type"] == "StatementFailed"
    assert result["error"]["state"] == "CANCELED"
    mock_workspace.statement_execution.cancel_execution.assert_called_with(
        "stmt-deadline-001"
    )


@pytest.mark.asyncio
async def test_databricks_statement_audit_event_links_request_to_statement(mock_workspace):
    """Every successful submit emits a `tool.databricks_statement` audit
    record carrying both the MCP request_id and the Databricks
    statement_id. That's the correlation an operator follows when
    tracing an alert from our audit log to system.query.history."""
    from mcp_server import audit

    mock_workspace.statement_execution.execute_statement.return_value = (
        _success_response(rows=[["main"]], columns=[("name", "STRING")])
    )

    with audit.capture() as records:
        token = audit.set_caller_id("operator-trace")
        try:
            await sql_tools.execute_sql_safe("SELECT 1")
        finally:
            audit.reset_caller_id(token)

    statement_evt = [r for r in records if r["event"] == "tool.databricks_statement"]
    assert len(statement_evt) == 1
    rec = statement_evt[0]
    assert rec["statement_id"] == "stmt-test"
    # request_id is the same UUID `_guard` set on tool.start
    starts = [r for r in records if r["event"] == "tool.start"]
    assert starts and rec["request_id"] == starts[0]["request_id"]
    # Tool name attribution
    assert rec["tool"] == "execute_sql_safe"


@pytest.mark.asyncio
async def test_warehouse_unavailable_becomes_structured_error(monkeypatch, mock_workspace):
    from mcp_server.databricks.client import WarehouseUnavailable

    def raise_unavailable(explicit=None):
        raise WarehouseUnavailable("no warehouse for testing")

    monkeypatch.setattr(
        "mcp_server.tools.sql_tools.resolve_warehouse_id", raise_unavailable
    )
    result = await sql_tools.execute_sql_safe("SELECT 1")
    assert result["error"]["type"] == "WarehouseUnavailable"
    mock_workspace.statement_execution.execute_statement.assert_not_called()
