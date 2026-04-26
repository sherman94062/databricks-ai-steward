"""Probe: compatibility with the official MCP Inspector.

Inspector (`@modelcontextprotocol/inspector`) is the reference debugging
tool from the MCP team. If our server passes Inspector's CLI checks, it
is correctly implementing the spec — Inspector is what most agent
harness authors test against.

Tests both stdio and streamable-http transports:
  * tools/list returns list_catalogs and health
  * tools/call list_catalogs returns the expected stub
  * tools/call health returns ready=true

Run:   python -m stress.probe_inspector_compat
Requires: node + npx (Inspector is an npm package fetched on first run).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time

INSPECTOR_PKG = "@modelcontextprotocol/inspector"
HTTP_PORT = 18767


def _run_inspector(args: list[str]) -> tuple[int, str]:
    """Run inspector CLI, returning (exit_code, stdout)."""
    cmd = ["npx", "-y", INSPECTOR_PKG, "--cli", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout


def _wait_for_port(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}{('  — ' + detail) if detail else ''}")
    return ok


def _verify_tools_list(stdout: str) -> bool:
    try:
        data = json.loads(stdout)
        names = sorted(t["name"] for t in data.get("tools", []))
        return _check(
            "tools/list", names == ["health", "list_catalogs"],
            f"got {names}",
        )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        return _check("tools/list", False, f"parse error: {e}")


def _verify_list_catalogs(stdout: str) -> bool:
    try:
        data = json.loads(stdout)
        text = data["content"][0]["text"]
        payload = json.loads(text)
        ok = "main" in payload.get("catalogs", []) and not data.get("isError")
        return _check("tools/call list_catalogs", ok, f"catalogs={payload.get('catalogs')}")
    except Exception as e:
        return _check("tools/call list_catalogs", False, f"parse error: {e}")


def _verify_health(stdout: str) -> bool:
    try:
        data = json.loads(stdout)
        text = data["content"][0]["text"]
        payload = json.loads(text)
        ok = payload.get("ready") is True and payload.get("status") == "ok"
        return _check("tools/call health",
                      ok, f"ready={payload.get('ready')} status={payload.get('status')}")
    except Exception as e:
        return _check("tools/call health", False, f"parse error: {e}")


def test_stdio() -> bool:
    print("[stdio]")
    server_cmd = ["--", sys.executable, "-m", "mcp_server.server"]
    common = ["--transport", "stdio"]

    rc, out = _run_inspector([*common, "--method", "tools/list", *server_cmd])
    a = _verify_tools_list(out)

    rc, out = _run_inspector([*common, "--method", "tools/call",
                              "--tool-name", "list_catalogs", *server_cmd])
    b = _verify_list_catalogs(out)

    rc, out = _run_inspector([*common, "--method", "tools/call",
                              "--tool-name", "health", *server_cmd])
    c = _verify_health(out)
    return a and b and c


def test_http() -> bool:
    print("[streamable-http]")
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_server.server",
         "--transport", "streamable-http", "--port", str(HTTP_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "MCP_LOG_LEVEL": "WARNING"},
    )
    try:
        if not _wait_for_port(HTTP_PORT, timeout=5.0):
            return _check("http server boot", False, f"port {HTTP_PORT} did not open")

        url = f"http://127.0.0.1:{HTTP_PORT}/mcp"
        common = [url, "--transport", "http"]

        rc, out = _run_inspector([*common, "--method", "tools/list"])
        a = _verify_tools_list(out)

        rc, out = _run_inspector([*common, "--method", "tools/call",
                                  "--tool-name", "list_catalogs"])
        b = _verify_list_catalogs(out)

        rc, out = _run_inspector([*common, "--method", "tools/call",
                                  "--tool-name", "health"])
        c = _verify_health(out)
        return a and b and c
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(2)


def main() -> int:
    if not shutil.which("npx"):
        print("FAIL: npx not found in PATH; install Node to run Inspector.")
        return 2

    stdio_ok = test_stdio()
    print()
    http_ok = test_http()

    print()
    if stdio_ok and http_ok:
        print("PASS — Inspector compatibility verified on stdio and streamable-http")
        return 0
    print("FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
