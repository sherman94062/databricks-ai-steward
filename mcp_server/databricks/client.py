"""Databricks workspace client wrapper.

Owns the singleton `WorkspaceClient` and provides a single helper for
calling its synchronous methods from async tools without blocking the
asyncio event loop.

Auth is read from `DATABRICKS_HOST` and `DATABRICKS_TOKEN` (loaded by
`mcp_server.server` via python-dotenv from `.env`). The SDK also
honors `~/.databrickscfg` profiles; if `DATABRICKS_HOST`/`_TOKEN` are
absent the SDK will fall back to a profile.
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


def get_workspace() -> WorkspaceClient:
    """Return the lazily-initialized singleton WorkspaceClient.

    The first call constructs the client (which reads credentials from
    env vars or `~/.databrickscfg`). Subsequent calls reuse it. Tests
    inject a mock by setting the module-level `_client` attribute via
    `set_workspace_for_tests(mock)`.
    """
    global _client
    if _client is None:
        _client = WorkspaceClient()
    return _client


def set_workspace_for_tests(client: Any) -> None:
    """Override the singleton, e.g. with a MagicMock. Pass `None` to reset."""
    global _client
    _client = client


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
