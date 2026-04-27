"""SDK fault-injection tests.

Mock the WorkspaceClient to raise each meaningful databricks-sdk error.
Verify each:
  * becomes a structured `{"error": {"type": ..., "message": ...}}` payload
  * does not crash the server
  * does not leak the workspace host or PAT
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

import httpx
from databricks.sdk.errors import (
    DatabricksError,
    InternalError,
    PermissionDenied,
    TemporarilyUnavailable,
    TooManyRequests,
    Unauthenticated,
)
from databricks.sdk.errors.sdk import OperationTimeout

from mcp_server.databricks import client as db_client
from mcp_server.tools import basic_tools


@pytest.fixture
def mock_workspace():
    ws = MagicMock()
    db_client.set_workspace_for_tests(ws)
    yield ws
    db_client.set_workspace_for_tests(None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc_factory, expected_type",
    [
        (lambda: Unauthenticated("token expired"), "Unauthenticated"),
        (lambda: PermissionDenied("user lacks USE_CATALOG"), "PermissionDenied"),
        (lambda: TooManyRequests("rate limit exceeded"), "TooManyRequests"),
        (lambda: InternalError("upstream 500"), "InternalError"),
        (lambda: TemporarilyUnavailable("service unavailable"), "TemporarilyUnavailable"),
        (lambda: OperationTimeout("operation took too long"), "OperationTimeout"),
        (lambda: DatabricksError("generic api error"), "DatabricksError"),
        (lambda: httpx.ReadTimeout("read timed out"), "ReadTimeout"),
        (lambda: httpx.ConnectError("connection refused"), "ConnectError"),
        (lambda: ValueError("malformed response"), "ValueError"),
    ],
    ids=[
        "401_unauthenticated",
        "403_permission_denied",
        "429_rate_limit",
        "500_internal",
        "503_unavailable",
        "operation_timeout",
        "generic_databricks_error",
        "network_read_timeout",
        "network_connect_error",
        "malformed_response",
    ],
)
async def test_sdk_error_becomes_structured_response(
    mock_workspace, exc_factory, expected_type
):
    mock_workspace.catalogs.list.side_effect = exc_factory()
    result = await basic_tools.list_catalogs()

    # Structured error, never a raised exception that would kill the server.
    assert "error" in result, f"expected structured error, got {result}"
    assert result["error"]["type"] == expected_type
    assert isinstance(result["error"]["message"], str)
    assert result["error"]["message"]   # non-empty


@pytest.mark.asyncio
async def test_sdk_error_message_does_not_leak_workspace_host(
    mock_workspace, monkeypatch
):
    """If the SDK error message embeds the host URL, the guard should redact it."""
    monkeypatch.setenv("DATABRICKS_HOST", "https://dbc-secret-workspace.cloud.databricks.com")
    mock_workspace.catalogs.list.side_effect = Unauthenticated(
        "auth failed for https://dbc-secret-workspace.cloud.databricks.com/api/2.1/unity-catalog/catalogs"
    )

    result = await basic_tools.list_catalogs()

    assert "dbc-secret-workspace" not in result["error"]["message"], (
        f"workspace host leaked: {result['error']['message']!r}"
    )


@pytest.mark.asyncio
async def test_sdk_error_message_does_not_leak_databricks_token(
    mock_workspace, monkeypatch
):
    """If the SDK error message embeds the token (it shouldn't, but defense-in-depth),
    the guard must redact it."""
    fake_token = "dapifaketoken12345"
    monkeypatch.setenv("DATABRICKS_TOKEN", fake_token)
    mock_workspace.catalogs.list.side_effect = Unauthenticated(
        f"bad credentials Bearer {fake_token}"
    )

    result = await basic_tools.list_catalogs()

    assert fake_token not in result["error"]["message"], (
        "PAT leaked through error path"
    )
