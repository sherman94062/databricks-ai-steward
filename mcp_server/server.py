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

from mcp_server.app import log, mcp
from mcp_server.lifecycle import run_with_lifecycle
from mcp_server.tools import basic_tools, health, sql_tools  # noqa: F401 — imported for side-effect of registering tools

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


def _build_starlette_app(transport: str):
    """Build the Starlette app for the given HTTP transport, wrapping
    with bearer-auth middleware if MCP_BEARER_TOKEN is set."""
    if transport == "sse":
        app = mcp.sse_app()
    else:  # streamable-http
        app = mcp.streamable_http_app()

    token = os.environ.get("MCP_BEARER_TOKEN", "").strip()
    bearer_caller = os.environ.get("MCP_BEARER_TOKEN_NAME", "").strip() or "bearer-authenticated"

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    from mcp_server import audit

    if token:
        expected = f"Bearer {token}"

        class _BearerAuth(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                got = request.headers.get("authorization", "")
                if not secrets.compare_digest(got, expected):
                    return JSONResponse(
                        {"error": "unauthorized"},
                        status_code=401,
                        headers={"WWW-Authenticate": 'Bearer realm="mcp"'},
                    )
                # Authenticated — set caller identity for the audit/
                # rate-limit hooks downstream. contextvars survives across
                # the await chain that handles this request.
                token_obj = audit.set_caller_id(bearer_caller)
                try:
                    return await call_next(request)
                finally:
                    audit.reset_caller_id(token_obj)

        app.add_middleware(_BearerAuth)
        log.warning("bearer auth enabled (token len=%d, caller_id=%r)",
                    len(token), bearer_caller)
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
