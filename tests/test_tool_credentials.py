"""Tests for the per-tool credential abstraction.

The contract (mcp_server/databricks/client.py):
  * If `MCP_TOOL_TOKEN_<TOOL>` is set for the currently-executing
    tool, `get_workspace()` returns a tool-scoped WorkspaceClient
    built with that token.
  * Per-tool clients are cached by `(tool, hash(token))`.
  * Tests using `set_workspace_for_tests` get a single mock —
    per-tool resolution is bypassed (deterministic test fixtures).
  * `_guard` sets the current tool name via
    `audit.set_current_tool(...)` before invoking the tool body.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from mcp_server import audit
from mcp_server.databricks import client as db_client


@pytest.fixture(autouse=True)
def _reset_client_state():
    db_client.set_workspace_for_tests(None)
    yield
    db_client.set_workspace_for_tests(None)


def test_default_path_uses_singleton(monkeypatch):
    """No per-tool token, no test override → constructor called once,
    returned client is reused on the second call."""
    monkeypatch.delenv("MCP_TOOL_TOKEN_FOO", raising=False)
    constructed: list[object] = []

    def _fake_ctor(*args, **kwargs):
        constructed.append((args, kwargs))
        return object()

    with patch("mcp_server.databricks.client.WorkspaceClient", side_effect=_fake_ctor):
        c1 = db_client.get_workspace()
        c2 = db_client.get_workspace()

    assert c1 is c2
    assert len(constructed) == 1


def test_per_tool_token_builds_distinct_client(monkeypatch):
    """When the contextvar names a tool with a configured token,
    get_workspace returns a *different* client than the default."""
    monkeypatch.setenv("DATABRICKS_HOST", "https://test.invalid")
    monkeypatch.setenv("MCP_TOOL_TOKEN_FOO", "tool-foo-token")

    def _fake_ctor(*args, **kwargs):
        return ("client", kwargs.get("token"))  # cheap sentinel

    with patch("mcp_server.databricks.client.WorkspaceClient", side_effect=_fake_ctor):
        # Default tool — singleton, no token kwarg passed
        default = db_client.get_workspace()

        # Set the context to "foo" — should pick up MCP_TOOL_TOKEN_FOO
        token = audit.set_current_tool("foo")
        try:
            foo_client = db_client.get_workspace()
        finally:
            audit.reset_current_tool(token)

    assert default != foo_client
    assert foo_client == ("client", "tool-foo-token")


def test_per_tool_clients_are_cached(monkeypatch):
    """Multiple calls under the same tool reuse the per-tool client."""
    monkeypatch.setenv("DATABRICKS_HOST", "https://test.invalid")
    monkeypatch.setenv("MCP_TOOL_TOKEN_FOO", "tool-foo-token")

    constructions: list[str] = []

    def _fake_ctor(*args, **kwargs):
        constructions.append(kwargs.get("token", "<default>"))
        return object()

    with patch("mcp_server.databricks.client.WorkspaceClient", side_effect=_fake_ctor):
        token = audit.set_current_tool("foo")
        try:
            c1 = db_client.get_workspace()
            c2 = db_client.get_workspace()
        finally:
            audit.reset_current_tool(token)

    assert c1 is c2
    # Exactly one construction with the per-tool token
    assert constructions == ["tool-foo-token"]


def test_token_rotation_rebuilds_client(monkeypatch):
    """Changing MCP_TOOL_TOKEN_FOO at runtime should produce a new
    client on the next call (not stick with the old token)."""
    monkeypatch.setenv("DATABRICKS_HOST", "https://test.invalid")
    monkeypatch.setenv("MCP_TOOL_TOKEN_FOO", "old-token")

    constructions: list[str] = []

    def _fake_ctor(*args, **kwargs):
        constructions.append(kwargs.get("token", "<default>"))
        return object()

    with patch("mcp_server.databricks.client.WorkspaceClient", side_effect=_fake_ctor):
        token = audit.set_current_tool("foo")
        try:
            db_client.get_workspace()
            monkeypatch.setenv("MCP_TOOL_TOKEN_FOO", "new-token")
            db_client.get_workspace()
        finally:
            audit.reset_current_tool(token)

    assert constructions == ["old-token", "new-token"]


def test_test_override_takes_precedence_over_per_tool_token(monkeypatch):
    """Test fixtures inject a single mock; per-tool tokens are ignored
    so existing test setups stay simple."""
    monkeypatch.setenv("MCP_TOOL_TOKEN_FOO", "would-be-tool-token")

    sentinel = object()
    db_client.set_workspace_for_tests(sentinel)

    token = audit.set_current_tool("foo")
    try:
        assert db_client.get_workspace() is sentinel
    finally:
        audit.reset_current_tool(token)


def test_guard_sets_current_tool_for_databricks_client():
    """End-to-end: a tool decorated with @_guard sees the correct
    `audit.current_tool()` value when its body runs."""
    from mcp_server.app import _guard

    captured: list[str | None] = []

    @_guard
    async def my_tool() -> dict:
        captured.append(audit.current_tool())
        return {"ok": True}

    import asyncio
    asyncio.run(my_tool())

    assert captured == ["my_tool"]
    # And by the time the wrapper returns, the contextvar is reset.
    assert audit.current_tool() is None
