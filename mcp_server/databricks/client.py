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
from typing import Any, Awaitable, Callable, TypeVar

from databricks.sdk import WorkspaceClient


T = TypeVar("T")

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


async def run_in_thread(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Dispatch a synchronous SDK call to a worker thread.

    `databricks-sdk` is sync-by-design. Calling it directly from an
    async tool would block the asyncio loop and (per `STRESS_FINDINGS`
    finding A1) wedge every other concurrent tool call on the session.
    `asyncio.to_thread` keeps the loop responsive while the SDK call
    runs.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)
