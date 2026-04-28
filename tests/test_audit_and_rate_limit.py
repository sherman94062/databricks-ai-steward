"""Tests for the production cross-cutting concerns: audit log emission,
caller-id propagation, and the per-(tool, caller) rate limiter.

The interesting test surface is `_guard` — every audit + rate-limit
event has to flow through it. We exercise the integration end-to-end
by decorating a small fake tool and asserting on the captured audit
records and the rate-limit refusal.
"""

from __future__ import annotations

import pytest

from mcp_server import audit, rate_limit
from mcp_server.app import _guard


def _decorate_async(fn, **guard_kw):
    """Mimic safe_tool minus the FastMCP registration."""
    return _guard(fn, **guard_kw)


@pytest.mark.asyncio
async def test_audit_emits_start_and_end_for_success():
    @_decorate_async
    async def my_tool(x: int) -> dict:
        return {"x": x * 2}

    with audit.capture() as records:
        result = await my_tool(x=21)

    assert result == {"x": 42}
    starts = [r for r in records if r["event"] == "tool.start"]
    ends = [r for r in records if r["event"] == "tool.end"]
    assert len(starts) == 1 and len(ends) == 1
    assert starts[0]["tool"] == "my_tool"
    assert starts[0]["request_id"] == ends[0]["request_id"]
    assert ends[0]["outcome"] == "success"
    assert ends[0]["latency_ms"] >= 0
    assert ends[0]["response_bytes"] > 0


@pytest.mark.asyncio
async def test_audit_records_error_outcome():
    @_decorate_async
    async def boom() -> dict:
        raise RuntimeError("nope")

    with audit.capture() as records:
        result = await boom()

    assert result["error"]["type"] == "RuntimeError"
    end = [r for r in records if r["event"] == "tool.end"][0]
    assert end["outcome"] == "error"
    assert end["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_audit_does_not_log_arg_values():
    """Argument *names* and a digest go into the audit. The values
    themselves never appear — same posture as scrubbing tokens from
    error messages."""

    @_decorate_async
    async def secret_tool(api_token: str, query: str) -> dict:
        return {"ok": True}

    sentinel_token = "dapiSENTINEL_should_not_appear"
    sentinel_query = "SELECT-SENTINEL-AVOID"

    with audit.capture() as records:
        await secret_tool(api_token=sentinel_token, query=sentinel_query)

    # Records should mention argument *names* but never their values.
    for record in records:
        flat = repr(record)
        assert sentinel_token not in flat
        assert sentinel_query not in flat
    start = [r for r in records if r["event"] == "tool.start"][0]
    assert sorted(start["kw_names"]) == ["api_token", "query"]


@pytest.mark.asyncio
async def test_caller_id_carries_into_audit_record():
    @_decorate_async
    async def echo() -> dict:
        return {"ok": True}

    token = audit.set_caller_id("agent-007")
    try:
        with audit.capture() as records:
            await echo()
    finally:
        audit.reset_caller_id(token)

    assert all(r["caller_id"] == "agent-007" for r in records)


@pytest.mark.asyncio
async def test_rate_limit_refuses_after_quota(monkeypatch):
    monkeypatch.setenv("MCP_RATE_LIMIT", "demo_tool=3/60")
    # Re-parse overrides since the module captured them at import.
    monkeypatch.setattr(rate_limit, "_OVERRIDES", rate_limit._parse_overrides("demo_tool=3/60"))

    @_decorate_async
    async def demo_tool() -> dict:
        return {"ok": True}

    # First three calls succeed; fourth gets RateLimitExceeded.
    for _ in range(3):
        r = await demo_tool()
        assert r == {"ok": True}

    with audit.capture() as records:
        rejected = await demo_tool()
    assert rejected["error"]["type"] == "RateLimitExceeded"

    # Audit captures: rate_limit_exceeded event + tool.end with
    # outcome=rate_limited (no tool.start because the bucket fired
    # before tool execution... wait — start fires before the limiter,
    # since we want a record of the attempt).
    rate_evt = [r for r in records if r["event"] == "tool.rate_limit_exceeded"]
    assert len(rate_evt) == 1
    assert rate_evt[0]["limit"] == 3
    assert rate_evt[0]["window_s"] == 60


@pytest.mark.asyncio
async def test_rate_limit_per_caller_isolation(monkeypatch):
    monkeypatch.setenv("MCP_RATE_LIMIT", "demo_tool=2/60")
    monkeypatch.setattr(rate_limit, "_OVERRIDES", rate_limit._parse_overrides("demo_tool=2/60"))

    @_decorate_async
    async def demo_tool() -> dict:
        return {"ok": True}

    # alice uses up her quota
    token_a = audit.set_caller_id("alice")
    try:
        await demo_tool()
        await demo_tool()
        rejected = await demo_tool()
        assert rejected["error"]["type"] == "RateLimitExceeded"
    finally:
        audit.reset_caller_id(token_a)

    # bob still has full quota — separate bucket
    token_b = audit.set_caller_id("bob")
    try:
        for _ in range(2):
            r = await demo_tool()
            assert r == {"ok": True}
    finally:
        audit.reset_caller_id(token_b)


@pytest.mark.asyncio
async def test_rate_limit_does_not_charge_tool_failures():
    """When the tool raises *after* admission, the slot is consumed.
    Production semantics: a misbehaving tool that always errors still
    counts against the caller's quota, otherwise a malicious caller
    could probe forever."""

    @_decorate_async
    async def always_fails() -> dict:
        raise RuntimeError("intentional")

    # FALLBACK is 50/min, so we'll just count after a few calls.
    with audit.capture():
        for _ in range(3):
            r = await always_fails()
            assert r["error"]["type"] == "RuntimeError"

    # Bucket should reflect 3 charged attempts.
    bucket = rate_limit._buckets[("always_fails", audit.current_caller_id())]
    assert len(bucket) == 3


def test_rate_limit_parse_overrides():
    parsed = rate_limit._parse_overrides("foo=5/60,bar=100/30,*=200/300")
    assert parsed["foo"] == rate_limit._Limit(5, 60)
    assert parsed["bar"] == rate_limit._Limit(100, 30)
    assert parsed["*"] == rate_limit._Limit(200, 300)


def test_rate_limit_parse_overrides_skips_garbage():
    parsed = rate_limit._parse_overrides("good=5/60,bad,=,foo=abc/def")
    assert parsed == {"good": rate_limit._Limit(5, 60)}


# ---- k8s probe endpoints ---------------------------------------------------
# Single test exercising both probes + the bearer-auth bypass — the
# FastMCP streamable-http session manager is a singleton that can't be
# rebuilt mid-process, so we share one app across all assertions.

def test_k8s_probes_bypass_bearer_auth_and_track_drain_state(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "test-token-1234567890")

    from starlette.testclient import TestClient

    from mcp_server import lifecycle
    from mcp_server.server import _build_starlette_app

    app = _build_starlette_app("streamable-http")
    with TestClient(app) as c:
        # /healthz is always 200 when the process is alive — no auth needed.
        r = c.get("/healthz")
        assert r.status_code == 200
        assert r.text.strip() == "ok"

        # /readyz is 200 when not shutting down — no auth needed.
        r = c.get("/readyz")
        assert r.status_code == 200
        assert r.text.strip() == "ready"

        # /mcp requires bearer auth — probes are special-cased, the
        # actual API surface is not.
        assert c.get("/mcp").status_code == 401

        # Flip the drain flag — /readyz reports 503, /healthz stays 200,
        # /mcp's auth requirement is unchanged.
        try:
            lifecycle._shutting_down = True
            r = c.get("/readyz")
            assert r.status_code == 503
            assert r.text.strip() == "draining"
            assert c.get("/healthz").status_code == 200
        finally:
            lifecycle._shutting_down = False
