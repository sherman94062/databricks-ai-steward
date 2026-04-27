"""execute_sql_safe tool tests.

Mocks the WorkspaceClient so tests never touch a live workspace.
Verifies governance + cap + warehouse-resolution + SDK-error handling.
"""

from __future__ import annotations

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
