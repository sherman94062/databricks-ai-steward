"""OpenTelemetry traces + Prometheus metrics for tool calls.

Both are *opt-in*. The runtime imports of `opentelemetry` and
`prometheus_client` are lazy so the production server can ship without
either dep installed. Activation:

  * **OTel traces** — set `OTEL_EXPORTER_OTLP_ENDPOINT=https://...` (or
    any other OTel SDK env var that triggers SDK auto-init). Spans are
    emitted as soon as the SDK is wired up, no other config needed.
  * **Prometheus metrics** — set `MCP_PROMETHEUS_ENABLED=1`. The
    `/metrics` route on HTTP transports starts returning counter +
    histogram values. The route bypasses bearer auth so scrapers don't
    need credentials.

If neither dep is installed and the env vars are unset, this module is
a complete no-op — `tool_span` returns a context manager that does
nothing, `record_tool_call` is a function that does nothing.

The module is loaded eagerly by `mcp_server.app`, so any import errors
fail loudly at startup rather than silently disabling observability.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator

_log = logging.getLogger("mcp_server.telemetry")

# ---- OpenTelemetry tracer (no-op fallback) --------------------------------

_otel_tracer: object | None = None  # opentelemetry.trace.Tracer once initialized
_otel_initialised = False


def _init_otel() -> None:
    """Construct a tracer if the SDK is installed and configured.

    Idempotent — safe to call from any number of paths. Sets
    `_otel_tracer` to the tracer on success or leaves it None.
    """
    global _otel_tracer, _otel_initialised
    if _otel_initialised:
        return
    _otel_initialised = True

    # The OTel SDK's auto-init reads OTEL_EXPORTER_OTLP_ENDPOINT et al.
    # from the environment, so we just need to call get_tracer once.
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") and \
       not os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        return  # No exporter configured — stay no-op.

    try:
        from opentelemetry import trace  # type: ignore[import-not-found]
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]
        from opentelemetry.sdk.trace.export import (  # type: ignore[import-not-found]
            BatchSpanProcessor,
        )
    except ImportError:
        _log.info("opentelemetry SDK not installed; tracing disabled")
        return

    resource = Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", "databricks-ai-steward"),
    })
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _otel_tracer = trace.get_tracer("mcp_server")
    _log.warning("OpenTelemetry tracing enabled — exporter=%s",
                 os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "<traces-only>"))


@contextlib.contextmanager
def tool_span(
    tool: str,
    request_id: str,
    caller: str,
) -> Iterator[None]:
    """Context manager wrapping a tool call in an OTel span.

    No-op if the OTel SDK isn't configured. The span carries
    {tool, request_id, caller} as attributes so traces correlate
    with audit log records.
    """
    _init_otel()
    if _otel_tracer is None:
        yield
        return

    with _otel_tracer.start_as_current_span(  # type: ignore[attr-defined]
        f"mcp.tool.{tool}",
        attributes={
            "mcp.tool.name": tool,
            "mcp.request.id": request_id,
            "mcp.caller.id": caller,
        },
    ) as span:
        try:
            yield
        except Exception as e:
            span.record_exception(e)
            span.set_status(_otel_status_error(str(e)))
            raise


def _otel_status_error(msg: str):
    """Lazy import — only used when OTel is active."""
    from opentelemetry.trace import Status, StatusCode  # type: ignore[import-not-found]
    return Status(StatusCode.ERROR, msg)


# ---- Prometheus metrics ---------------------------------------------------

_prom_enabled = os.environ.get("MCP_PROMETHEUS_ENABLED", "").strip() in ("1", "true", "yes")
_prom_calls = None         # Counter
_prom_duration = None      # Histogram
_prom_in_flight = None     # Gauge


def _init_prometheus() -> None:
    global _prom_calls, _prom_duration, _prom_in_flight
    if not _prom_enabled or _prom_calls is not None:
        return
    try:
        from prometheus_client import Counter, Gauge, Histogram  # type: ignore[import-not-found]
    except ImportError:
        _log.info("prometheus_client not installed; metrics disabled")
        return

    _prom_calls = Counter(
        "mcp_tool_calls_total",
        "Total MCP tool calls broken down by tool, caller, and outcome.",
        labelnames=("tool", "caller", "outcome"),
    )
    _prom_duration = Histogram(
        "mcp_tool_call_duration_seconds",
        "Tool call latency, including governance / rate-limit time.",
        labelnames=("tool", "outcome"),
        # Spans the relevant range: <50ms metadata reads through 60s SQL queries.
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
    )
    _prom_in_flight = Gauge(
        "mcp_in_flight_tools",
        "Currently-executing MCP tool calls in this process.",
    )
    _log.warning("Prometheus metrics enabled — scrape /metrics on the HTTP transport")


def record_tool_call(
    tool: str,
    caller: str,
    outcome: str,
    duration_s: float,
) -> None:
    """Record one completed tool call. No-op if metrics aren't enabled."""
    _init_prometheus()
    if _prom_calls is None:
        return
    _prom_calls.labels(tool=tool, caller=caller, outcome=outcome).inc()
    if _prom_duration is not None:
        _prom_duration.labels(tool=tool, outcome=outcome).observe(duration_s)


def in_flight_inc() -> None:
    _init_prometheus()
    if _prom_in_flight is not None:
        _prom_in_flight.inc()


def in_flight_dec() -> None:
    if _prom_in_flight is not None:
        _prom_in_flight.dec()


def prometheus_app():
    """Return a Starlette ASGI app exposing GET /metrics, or None if
    Prometheus integration is disabled."""
    _init_prometheus()
    if _prom_calls is None:
        return None
    try:
        from prometheus_client import (  # type: ignore[import-not-found]
            CONTENT_TYPE_LATEST,
            generate_latest,
        )
        from starlette.responses import Response
    except ImportError:
        return None

    async def _metrics(_request):
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return _metrics


def _reset_for_tests() -> None:
    """Tests use this to flush state between cases.

    For Prometheus: also unregister from the process-global
    `CollectorRegistry`, otherwise re-running `_init_prometheus`
    raises `ValueError: Duplicated timeseries`.
    """
    global _otel_tracer, _otel_initialised
    global _prom_calls, _prom_duration, _prom_in_flight

    _otel_tracer = None
    _otel_initialised = False

    if _prom_calls is not None or _prom_duration is not None or _prom_in_flight is not None:
        try:
            from prometheus_client import REGISTRY
            for c in (_prom_calls, _prom_duration, _prom_in_flight):
                if c is not None:
                    try:
                        REGISTRY.unregister(c)
                    except KeyError:
                        pass
        except ImportError:
            pass

    _prom_calls = None
    _prom_duration = None
    _prom_in_flight = None
