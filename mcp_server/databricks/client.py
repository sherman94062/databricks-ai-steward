"""Databricks workspace client wrapper.

Owns the singleton `WorkspaceClient` (one per token) and provides a
single helper for calling its synchronous methods from async tools
without blocking the asyncio event loop.

Auth precedence — read at call time, so token rotation in `.env` /
secrets-injector takes effect on the next request:

  1. **Per-tool token** — if the currently-executing tool is named
     `foo` and `MCP_TOOL_TOKEN_FOO` is set, that token is used. The
     production-grade pattern for least-privilege: provision one
     Databricks service principal per tool with only the grants that
     tool needs (e.g. `execute_sql_safe` gets `USE_CATALOG` on
     production but no `MODIFY`; `recent_audit_events` gets `SELECT`
     on `system.access.audit` only). The steward then never holds a
     blast-radius credential.
  2. **Default token** — `DATABRICKS_TOKEN` env var.
  3. **SDK profile** — `~/.databrickscfg` if neither env var is set.

Per-tool clients are cached by tool name + token-hash, so a token
rotation rebuilds the client on next request without keeping stale
HTTPS pools around.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import State as WarehouseState


class WarehouseUnavailable(RuntimeError):
    """Raised when no Databricks SQL warehouse is reachable for execute_sql_safe.

    Distinguishes the resolver's three failure modes — explicit ID didn't
    exist, env var pointed at nothing, or no RUNNING warehouse exists —
    so the tool layer can surface a clean structured error.
    """

_client: WorkspaceClient | None = None
# Distinguishes "tests injected a mock via set_workspace_for_tests"
# from "we lazily constructed the default singleton". When this flag
# is True, _client is the test override and bypasses per-tool tokens.
_test_override_active: bool = False
# Cache of tool-scoped clients, keyed by `f"{tool_name}:{hash(token)}"`
# so rotating the token rebuilds the client on next call.
_tool_clients: dict[str, WorkspaceClient] = {}


def _tool_token_for(tool: str | None) -> str:
    """Return the configured per-tool token, or "" if none is set."""
    if not tool:
        return ""
    var = f"MCP_TOOL_TOKEN_{tool.upper()}"
    return os.environ.get(var, "").strip()


def _build_tool_client(token: str) -> WorkspaceClient:
    host = os.environ.get("DATABRICKS_HOST", "").strip()
    if not host:
        # Fall back to SDK auto-detection — covers the .databrickscfg
        # profile case. SDK will raise loudly if it can't find a host.
        return WorkspaceClient(token=token)
    return WorkspaceClient(host=host, token=token)


def get_workspace() -> WorkspaceClient:
    """Return a WorkspaceClient appropriate for the currently-executing tool.

    If `MCP_TOOL_TOKEN_<TOOL>` is set for the current tool (set by
    `_guard` on entry via `audit.set_current_tool`), build (or reuse)
    a tool-scoped client with that token. Otherwise return the lazily-
    constructed default singleton.

    Tests inject a mock via `set_workspace_for_tests(mock)` — the mock
    overrides everything (per-tool tokens are ignored in test mode so
    fixtures stay simple).
    """
    global _client

    # Test override takes precedence — keeps unit tests deterministic.
    if _test_override_active:
        return _client  # type: ignore[return-value]

    # Per-tool credential path. Checked before the default singleton so
    # that even if the default has already been constructed, a tool
    # with its own token gets the more-restrictive client.
    from mcp_server import audit  # local import to avoid circular at module load
    tool = audit.current_tool()
    tool_token = _tool_token_for(tool)
    if tool and tool_token:
        cache_key = f"{tool}:{hash(tool_token)}"
        cached = _tool_clients.get(cache_key)
        if cached is not None:
            return cached
        client = _build_tool_client(tool_token)
        _tool_clients[cache_key] = client
        return client

    # Default: lazy singleton built from DATABRICKS_TOKEN / profile.
    if _client is None:
        _client = WorkspaceClient()
    return _client


def set_workspace_for_tests(client: Any) -> None:
    """Override the singleton, e.g. with a MagicMock. Pass `None` to reset."""
    global _client, _tool_clients, _test_override_active
    _client = client
    _test_override_active = client is not None
    if client is None:
        # Also flush the per-tool cache so a previous test's per-tool
        # client doesn't bleed across.
        _tool_clients.clear()


async def run_in_thread[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Dispatch a synchronous SDK call to a worker thread.

    `databricks-sdk` is sync-by-design. Calling it directly from an
    async tool would block the asyncio loop and (per `STRESS_FINDINGS`
    finding A1) wedge every other concurrent tool call on the session.
    `asyncio.to_thread` keeps the loop responsive while the SDK call
    runs.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


def resolve_warehouse_id(explicit: str | None = None) -> str:
    """Resolve a SQL warehouse ID by precedence:
        1. caller-supplied `explicit` (used as-is, no validation here —
           SDK will fail loudly if it's bogus)
        2. MCP_DATABRICKS_WAREHOUSE_ID env var
        3. first warehouse in workspace whose state is RUNNING

    Raises `WarehouseUnavailable` if all three fail. Synchronous —
    callers should wrap with `run_in_thread`.
    """
    if explicit:
        return explicit

    from_env = os.environ.get("MCP_DATABRICKS_WAREHOUSE_ID", "").strip()
    if from_env:
        return from_env

    ws = get_workspace()
    all_warehouses = [w for w in ws.warehouses.list() if w.id]
    running = [w for w in all_warehouses if w.state == WarehouseState.RUNNING]
    # mypy can't narrow .id from `str | None` through the truthiness
    # filter above, so we re-check explicitly. (asserts are bandit-flagged
    # since they're stripped under -O.)
    if running and running[0].id is not None:
        return running[0].id

    # Fallback: any warehouse at all, even if STOPPED — Databricks will
    # auto-start it on first statement, which is slow but correct.
    if all_warehouses and all_warehouses[0].id is not None:
        return all_warehouses[0].id

    raise WarehouseUnavailable(
        "no SQL warehouse available — set MCP_DATABRICKS_WAREHOUSE_ID or pass "
        "warehouse_id explicitly, and confirm at least one warehouse exists in "
        "the workspace"
    )
