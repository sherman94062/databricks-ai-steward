"""Tests for the OpenTelemetry + Prometheus integration.

The runtime imports of `opentelemetry` and `prometheus_client` are
both lazy and optional. CI installs them so we exercise both the
disabled (no env var configured) and the enabled paths.
"""

from __future__ import annotations

import pytest

from mcp_server import audit, telemetry
from mcp_server.app import _guard


@pytest.fixture(autouse=True)
def _reset_telemetry_state():
    telemetry._reset_for_tests()
    yield
    telemetry._reset_for_tests()


# ---- OpenTelemetry traces -------------------------------------------------


def test_tool_span_is_noop_when_otel_not_configured():
    """Default state: OTEL_EXPORTER_OTLP_ENDPOINT unset → tool_span
    is a context manager that does nothing and never raises."""
    with telemetry.tool_span("any_tool", "req-1", "alice"):
        pass  # no error


def test_tool_span_is_noop_when_only_traces_endpoint_unset(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    with telemetry.tool_span("any_tool", "req-1", "alice"):
        pass


def test_otel_initializes_when_endpoint_configured(monkeypatch):
    """With OTEL_EXPORTER_OTLP_ENDPOINT set, the SDK is imported and
    a Tracer is constructed. We patch the OTLP exporter with an
    in-memory one so the test doesn't generate retry-network chatter."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-test.invalid:4318")

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    in_memory = InMemorySpanExporter()

    # Replace the OTLP exporter import inside _init_otel with one that
    # returns the in-memory exporter — keeps the network silent.
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
        lambda *a, **kw: in_memory,
    )

    telemetry._reset_for_tests()
    with telemetry.tool_span("execute_sql_safe", "req-2", "bob"):
        pass

    assert telemetry._otel_tracer is not None
    # Force flush so the span is captured before assertion.
    from opentelemetry import trace
    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush(timeout_millis=1000)
    spans = in_memory.get_finished_spans()
    assert any(s.name == "mcp.tool.execute_sql_safe" for s in spans)


# ---- Prometheus metrics ---------------------------------------------------


def test_record_tool_call_is_noop_when_metrics_disabled():
    """With MCP_PROMETHEUS_ENABLED unset, record_tool_call is a no-op
    and prometheus_app() returns None."""
    telemetry._reset_for_tests()
    telemetry.record_tool_call("foo", "alice", "success", 0.123)
    assert telemetry.prometheus_app() is None


def test_record_tool_call_increments_counter_when_enabled(monkeypatch):
    monkeypatch.setenv("MCP_PROMETHEUS_ENABLED", "1")
    # Re-evaluate the env-driven flag on the module.
    monkeypatch.setattr(telemetry, "_prom_enabled", True)
    telemetry._reset_for_tests()

    telemetry.record_tool_call("foo", "alice", "success", 0.5)
    telemetry.record_tool_call("foo", "alice", "success", 0.7)
    telemetry.record_tool_call("foo", "alice", "error", 1.0)

    # Pull the counter value via prometheus_client's exposition format.
    from prometheus_client import generate_latest
    out = generate_latest().decode()

    assert 'mcp_tool_calls_total{caller="alice",outcome="success",tool="foo"} 2.0' in out
    assert 'mcp_tool_calls_total{caller="alice",outcome="error",tool="foo"} 1.0' in out
    assert "mcp_tool_call_duration_seconds_bucket" in out


def test_metrics_route_exposed_when_enabled(monkeypatch):
    """The /metrics route appears on the HTTP app when Prometheus is on."""
    monkeypatch.setenv("MCP_PROMETHEUS_ENABLED", "1")
    monkeypatch.setattr(telemetry, "_prom_enabled", True)
    telemetry._reset_for_tests()

    handler = telemetry.prometheus_app()
    assert handler is not None and callable(handler)


def test_metrics_route_is_none_when_disabled(monkeypatch):
    monkeypatch.delenv("MCP_PROMETHEUS_ENABLED", raising=False)
    monkeypatch.setattr(telemetry, "_prom_enabled", False)
    telemetry._reset_for_tests()

    assert telemetry.prometheus_app() is None


# ---- Integration: _guard records both metrics + spans --------------------


@pytest.mark.asyncio
async def test_guard_increments_prometheus_on_success(monkeypatch):
    monkeypatch.setenv("MCP_PROMETHEUS_ENABLED", "1")
    monkeypatch.setattr(telemetry, "_prom_enabled", True)
    telemetry._reset_for_tests()

    @_guard
    async def my_tool() -> dict:
        return {"ok": True}

    token = audit.set_caller_id("integration-test")
    try:
        await my_tool()
    finally:
        audit.reset_caller_id(token)

    from prometheus_client import generate_latest
    out = generate_latest().decode()
    assert 'mcp_tool_calls_total{caller="integration-test",outcome="success",tool="my_tool"} 1.0' in out


@pytest.mark.asyncio
async def test_guard_increments_prometheus_on_error(monkeypatch):
    monkeypatch.setenv("MCP_PROMETHEUS_ENABLED", "1")
    monkeypatch.setattr(telemetry, "_prom_enabled", True)
    telemetry._reset_for_tests()

    @_guard
    async def boom() -> dict:
        raise RuntimeError("nope")

    token = audit.set_caller_id("err-test")
    try:
        await boom()
    finally:
        audit.reset_caller_id(token)

    from prometheus_client import generate_latest
    out = generate_latest().decode()
    assert 'mcp_tool_calls_total{caller="err-test",outcome="error",tool="boom"} 1.0' in out
