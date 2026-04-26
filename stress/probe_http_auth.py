"""Probe: bearer-token auth on HTTP transports.

Spawns the production server with MCP_BEARER_TOKEN set, then verifies:
  * unauthenticated request returns 401
  * wrong token returns 401
  * correct token returns 200 / 202

Uses raw httpx since the MCP SDK ClientSession would need to be told
how to inject auth headers, and we want to test the middleware here.

Run:   python -m stress.probe_http_auth
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import httpx


PORT = 18769
URL = f"http://127.0.0.1:{PORT}/mcp"
TOKEN = "probe-secret-test-token"


def _wait_for_port(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _post_initialize(headers: dict) -> int:
    """Send a minimal initialize POST. Returns HTTP status code."""
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "probe-auth", "version": "0"},
        },
    }
    common = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    common.update(headers)
    try:
        r = httpx.post(URL, json=body, headers=common, timeout=3.0)
        return r.status_code
    except httpx.HTTPError as e:
        print(f"[error] {type(e).__name__}: {e}")
        return -1


def main() -> int:
    env = {**os.environ, "MCP_BEARER_TOKEN": TOKEN, "MCP_LOG_LEVEL": "WARNING"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_server.server",
         "--transport", "streamable-http", "--port", str(PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        if not _wait_for_port(PORT, timeout=5.0):
            print(f"FAIL: server did not bind {PORT}")
            return 1

        # 1. No auth header → 401
        s1 = _post_initialize({})
        # 2. Wrong token → 401
        s2 = _post_initialize({"Authorization": "Bearer wrong-token"})
        # 3. Correct token → 200 or 202 (initialize succeeded; SSE may use 202)
        s3 = _post_initialize({"Authorization": f"Bearer {TOKEN}"})

        print(f"no auth          → {s1}")
        print(f"wrong token      → {s2}")
        print(f"correct token    → {s3}")

        ok = s1 == 401 and s2 == 401 and 200 <= s3 < 300
        print()
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(2)


if __name__ == "__main__":
    sys.exit(main())
