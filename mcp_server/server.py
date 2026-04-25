"""Entry point. Selects a transport and delegates to FastMCP.

Default transport is stdio (used by Claude Code, Claude Desktop, Cursor's
local-MCP mode). HTTP transports are for hosted / web-based agent harnesses:
  * sse              — Server-Sent Events; older spec, widely supported
  * streamable-http  — newer MCP spec, single-endpoint; preferred for new clients

Configuration (CLI flag or env var):
  --transport / MCP_TRANSPORT   stdio | sse | streamable-http   (default: stdio)
  --host      / MCP_HOST        bind host for HTTP transports   (default: 127.0.0.1)
  --port      / MCP_PORT        bind port for HTTP transports   (default: 8765)
"""

from __future__ import annotations

import argparse
import asyncio
import os

from dotenv import load_dotenv

from mcp_server.app import log, mcp
from mcp_server.lifecycle import run_with_lifecycle
from mcp_server.tools import basic_tools, health  # noqa: F401 — imported for side-effect of registering tools

load_dotenv()


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
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    if args.transport == "stdio":
        # Custom lifecycle: signal handlers, bounded grace, cleanup callbacks.
        # Required because asyncio cancellation cannot interrupt the anyio
        # stdin-reader thread; we close stdin to force EOF.
        asyncio.run(run_with_lifecycle(mcp))
        return

    # HTTP transports: uvicorn handles SIGTERM/SIGINT graceful shutdown
    # natively (drains in-flight requests, then exits). We don't install
    # our own handler in this path to avoid double-shutdown.
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    log.warning("starting MCP server on %s://%s:%d", args.transport, args.host, args.port)

    if args.transport == "sse":
        asyncio.run(mcp.run_sse_async())
    else:  # streamable-http
        asyncio.run(mcp.run_streamable_http_async())


if __name__ == "__main__":
    main()
