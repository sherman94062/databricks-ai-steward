"""Entry point. Selects a transport and delegates to FastMCP.

Default transport is stdio (used by Claude Code, Claude Desktop, Cursor's
local-MCP mode). HTTP transports are for hosted / web-based agent harnesses:
  * sse              — Server-Sent Events; older spec, widely supported
  * streamable-http  — newer MCP spec, single-endpoint; preferred for new clients

Configuration (CLI flag or env var):
  --transport       / MCP_TRANSPORT       stdio | sse | streamable-http   (default: stdio)
  --host            / MCP_HOST            bind host for HTTP transports    (default: 127.0.0.1)
  --port            / MCP_PORT            bind port for HTTP transports    (default: 8765)
  --allow-external  / MCP_ALLOW_EXTERNAL  required to bind non-loopback    (default: off)
                    / MCP_BEARER_TOKEN    if set, HTTP requires `Authorization: Bearer <token>`
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys

from dotenv import load_dotenv

from mcp_server import prompts  # noqa: F401 — imported for side-effect of registering prompts
from mcp_server.app import log, mcp
from mcp_server.lifecycle import run_with_lifecycle
from mcp_server.tools import (  # noqa: F401 — imported for side-effect of registering tools
    basic_tools,
    health,
    sql_tools,
)

load_dotenv()


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="mcp_server.server")
    p.add_argument(
        "--transport",
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        choices=["stdio", "sse", "streamable-http"],
        help="MCP transport to expose. Default: stdio.",
    )
    p.add_argument(
        "--host",
        default=os.environ.get("MCP_HOST", "127.0.0.1"),
        help="Bind host for HTTP transports. Default: 127.0.0.1 (loopback).",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MCP_PORT", "8765")),
        help="Bind port for HTTP transports. Default: 8765.",
    )
    p.add_argument(
        "--allow-external",
        action="store_true",
        default=os.environ.get("MCP_ALLOW_EXTERNAL", "").lower() in ("1", "true", "yes"),
        help="Permit non-loopback bind. Required for any host other than "
             "127.0.0.1 / localhost / ::1.",
    )
    return p.parse_args()


def make_bearer_auth_middleware(
    *,
    expected_authorization: str,
    bearer_caller: str,
    trust_end_user_header: bool,
    end_user_header_name: str,
    unauthenticated_paths: frozenset[str],
):
    """Build the bearer-auth Starlette BaseHTTPMiddleware class.

    Extracted from `_build_starlette_app` so tests can apply it to a
    plain Starlette app instead of FastMCP's streamable-http app
    (which uses a singleton session manager that can't be re-entered
    across tests).
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    from mcp_server import audit

    class _BearerAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path in unauthenticated_paths:
                return await call_next(request)
            got = request.headers.get("authorization", "")
            if not secrets.compare_digest(got, expected_authorization):
                return JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="mcp"'},
                )
            # Authenticated — set caller identity for the audit /
            # rate-limit hooks downstream. contextvars survives
            # across the await chain that handles this request.
            # Caller resolution priority:
            #   1. <end-user> from the trusted header, if enabled +
            #      present + non-empty.
            #   2. bearer_caller (from MCP_BEARER_TOKEN_NAME).
            caller = bearer_caller
            if trust_end_user_header:
                end_user = request.headers.get(end_user_header_name, "").strip()
                if end_user:
                    caller = end_user
            token_obj = audit.set_caller_id(caller)
            try:
                return await call_next(request)
            finally:
                audit.reset_caller_id(token_obj)

    return _BearerAuth


def _build_starlette_app(transport: str):
    """Build the Starlette app for the given HTTP transport, wrapping
    with k8s probe routes (always) and bearer-auth middleware (if
    MCP_BEARER_TOKEN is set)."""
    if transport == "sse":
        app = mcp.sse_app()
    else:  # streamable-http
        app = mcp.streamable_http_app()

    token = os.environ.get("MCP_BEARER_TOKEN", "").strip()
    bearer_caller = os.environ.get("MCP_BEARER_TOKEN_NAME", "").strip() or "bearer-authenticated"
    # Per-end-user identity propagation. When the steward sits behind
    # a trusted upstream proxy (oauth2-proxy / authelia / istio /
    # API-gateway with verified user headers), the proxy sets a header
    # naming the authenticated end-user. This server picks it up and
    # uses it as caller_id IFF the operator explicitly trusts the
    # header — otherwise an attacker could just forge it.
    #
    # Default is OFF (fail-secure). To enable, the operator must set
    # both:
    #   MCP_TRUST_END_USER_HEADER=1
    #   MCP_END_USER_HEADER=<header-name>   (default X-End-User)
    # AND must guarantee that the upstream proxy strips any
    # client-supplied value of that header before setting its own —
    # otherwise the trust is undermined.
    trust_end_user_header = os.environ.get("MCP_TRUST_END_USER_HEADER", "").strip().lower() in ("1", "true", "yes")
    end_user_header_name = os.environ.get("MCP_END_USER_HEADER", "X-End-User").strip()

    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from mcp_server import lifecycle

    # ---- k8s probe routes ------------------------------------------------
    # /healthz: process is alive. Used as the liveness probe — k8s restarts
    #           the pod if this fails repeatedly.
    # /readyz:  process can serve new requests. Returns 503 once shutdown
    #           is signalled so a rolling-update load balancer drains us
    #           before SIGKILL. Used as the readiness probe.
    # Both bypass bearer auth (operators don't have the token; orchestrators
    # need to probe regardless of auth state).

    async def _healthz(_request):
        return PlainTextResponse("ok\n")

    async def _readyz(_request):
        if lifecycle.is_shutting_down():
            return PlainTextResponse("draining\n", status_code=503)
        return PlainTextResponse("ready\n")

    # FastMCP's app already has an internal route table; we prepend ours
    # so they take precedence and bypass the bearer-auth middleware
    # added below.
    app.routes.insert(0, Route("/healthz", _healthz, methods=["GET"]))
    app.routes.insert(1, Route("/readyz", _readyz, methods=["GET"]))

    # ---- /metrics (Prometheus) ------------------------------------------
    # Opt-in via MCP_PROMETHEUS_ENABLED. If enabled and the
    # prometheus_client dep is installed, we expose /metrics. Bypasses
    # bearer auth so scrapers don't need credentials — same posture as
    # /healthz and /readyz, and the metric values are not sensitive
    # (they're aggregates, no caller-supplied data).
    from mcp_server import telemetry
    metrics_handler = telemetry.prometheus_app()
    if metrics_handler is not None:
        app.routes.insert(2, Route("/metrics", metrics_handler, methods=["GET"]))
        log.warning("Prometheus /metrics enabled")

    # ---- bearer auth -----------------------------------------------------
    if token:
        # Paths the auth middleware lets through unauthenticated. Probes
        # and metrics belong to the orchestrator (k8s, scrapers), not
        # the API surface, so they're always accessible.
        unauthenticated_paths = frozenset({"/healthz", "/readyz", "/metrics"})

        middleware_cls = make_bearer_auth_middleware(
            expected_authorization=f"Bearer {token}",
            bearer_caller=bearer_caller,
            trust_end_user_header=trust_end_user_header,
            end_user_header_name=end_user_header_name,
            unauthenticated_paths=unauthenticated_paths,
        )
        app.add_middleware(middleware_cls)
        log.warning("bearer auth enabled (token len=%d, caller_id=%r)",
                    len(token), bearer_caller)
        if trust_end_user_header:
            log.warning(
                "trusting %r header for per-end-user caller_id "
                "(MUST be set by a verified upstream proxy)",
                end_user_header_name,
            )
    else:
        log.warning("bearer auth NOT enabled (set MCP_BEARER_TOKEN to enable)")

    return app


async def _run_http(transport: str, host: str, port: int) -> None:
    import uvicorn

    app = _build_starlette_app(transport)
    config = uvicorn.Config(app, host=host, port=port,
                            log_level=os.environ.get("MCP_LOG_LEVEL", "warning").lower())
    server = uvicorn.Server(config)
    await server.serve()


def main() -> None:
    args = _parse_args()

    if args.transport == "stdio":
        # Custom lifecycle: signal handlers, bounded grace, cleanup
        # callbacks. Required because asyncio cancellation cannot
        # interrupt the anyio stdin-reader thread; we close stdin to
        # force EOF.
        asyncio.run(run_with_lifecycle(mcp))
        return

    # HTTP path: refuse external bind unless explicitly allowed.
    if args.host not in LOOPBACK_HOSTS and not args.allow_external:
        sys.exit(
            f"Refusing to bind '{args.host}': external bind requires "
            f"--allow-external (or MCP_ALLOW_EXTERNAL=1). Default loopback "
            f"is 127.0.0.1. If you do bind externally, set MCP_BEARER_TOKEN "
            f"and run behind TLS — this server has no other auth."
        )

    if args.host not in LOOPBACK_HOSTS and not os.environ.get("MCP_BEARER_TOKEN"):
        log.warning(
            "binding %s without MCP_BEARER_TOKEN — anyone reachable can "
            "call any tool. Strongly recommended: set MCP_BEARER_TOKEN.",
            args.host,
        )

    log.warning("starting MCP server on %s://%s:%d", args.transport, args.host, args.port)
    asyncio.run(_run_http(args.transport, args.host, args.port))


if __name__ == "__main__":
    main()
