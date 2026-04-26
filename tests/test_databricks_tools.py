"""Tests for the Databricks-backed tools.

Mocks the WorkspaceClient singleton so tests never touch a live workspace.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mcp_server.databricks import client as db_client
from mcp_server.tools import basic_tools


def _fake_catalog(name: str, type_value: str | None = None, comment: str | None = None):
    """Mimic the bits of databricks.sdk.service.catalog.CatalogInfo we read."""
    return SimpleNamespace(
        name=name,
        catalog_type=SimpleNamespace(value=type_value) if type_value else None,
        comment=comment,
    )


@pytest.fixture
def mock_workspace():
    ws = MagicMock()
    db_client.set_workspace_for_tests(ws)
    yield ws
    db_client.set_workspace_for_tests(None)


@pytest.mark.asyncio
async def test_list_catalogs_returns_structured_payload(mock_workspace):
    mock_workspace.catalogs.list.return_value = iter([
        _fake_catalog("system", "SYSTEM_CATALOG", "auto-created"),
        _fake_catalog("workspace", "MANAGED_CATALOG", None),
    ])
    result = await basic_tools.list_catalogs()
    assert result == {
        "catalogs": [
            {"name": "system", "type": "SYSTEM_CATALOG", "comment": "auto-created"},
            {"name": "workspace", "type": "MANAGED_CATALOG", "comment": None},
        ],
    }


@pytest.mark.asyncio
async def test_list_catalogs_handles_missing_catalog_type(mock_workspace):
    """Some catalog types serialize as None; the tool must not crash."""
    mock_workspace.catalogs.list.return_value = iter([_fake_catalog("foo")])
    result = await basic_tools.list_catalogs()
    assert result["catalogs"] == [{"name": "foo", "type": None, "comment": None}]


@pytest.mark.asyncio
async def test_list_catalogs_propagates_sdk_error_through_guard(mock_workspace):
    """SDK exceptions should land as a structured error, not crash the server."""
    mock_workspace.catalogs.list.side_effect = RuntimeError("workspace unreachable")
    result = await basic_tools.list_catalogs()
    assert result == {"error": {"type": "RuntimeError", "message": "workspace unreachable"}}
