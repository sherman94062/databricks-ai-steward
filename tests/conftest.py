"""Shared pytest fixtures.

Auto-resets cross-test state that the production code intentionally
makes process-global (rate-limiter buckets, audit caller_id contextvar)
so each test starts clean."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limit_buckets():
    from mcp_server import rate_limit
    rate_limit.reset_for_tests()
    yield
    rate_limit.reset_for_tests()


@pytest.fixture(autouse=True)
def _silence_audit_stderr(monkeypatch):
    """Don't pollute test output with audit records. Tests that *want*
    to assert on audit records use `with audit.capture()` explicitly."""
    monkeypatch.setenv("MCP_AUDIT_DISABLE_STDERR", "1")
    yield
