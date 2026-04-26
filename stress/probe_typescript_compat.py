"""Probe: compatibility with the official MCP TypeScript SDK.

Most non-Python clients (Claude Desktop, Cursor, Cline, the TypeScript
inspector frontends) sit on top of `@modelcontextprotocol/sdk` for Node.
If our server passes a TS SDK round-trip — initialize, tools/list,
list_catalogs, health — we have strong cross-language evidence the
spec implementation is correct, not just an artifact of the Python SDK
that drives our other probes.

Tests stdio and streamable-http transports.

Run:   python -m stress.probe_typescript_compat
Requires: node + npx (one-time `npm install` runs automatically below).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TS_DIR = REPO_ROOT / "stress" / "ts"
HTTP_PORT = 18771


def _wait_for_port(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _ensure_node_modules() -> bool:
    """Install npm deps if missing. Returns True on success."""
    if (TS_DIR / "node_modules" / "@modelcontextprotocol" / "sdk").exists():
        return True
    print("[setup] running npm install in stress/ts/ ...", file=sys.stderr)
    proc = subprocess.run(
        ["npm", "install", "--silent"],
        cwd=str(TS_DIR), capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        print(f"FAIL: npm install failed:\n{proc.stderr}")
        return False
    return True


def _run_ts(args: list[str], extra_env: dict | None = None) -> dict | None:
    """Invoke compat.ts with the given args; parse the trailing JSON line."""
    env = {**os.environ, **(extra_env or {})}
    proc = subprocess.run(
        ["npx", "tsx", "compat.ts", *args],
        cwd=str(TS_DIR), capture_output=True, text=True,
        timeout=60, env=env,
    )
    # The TS probe prints a single JSON line on stdout.
    last_line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    try:
        return json.loads(last_line)
    except json.JSONDecodeError:
        print(f"FAIL: could not parse TS output\nstdout: {proc.stdout}\nstderr: {proc.stderr}")
        return None


def _print_verdict(verdict: dict) -> bool:
    print(f"[{verdict['transport']}]")
    for step in verdict["steps"]:
        mark = "✓" if step["ok"] else "✗"
        detail = step.get("detail", "")
        print(f"  {mark} {step['name']}{('  — ' + detail) if detail else ''}")
    return bool(verdict["ok"])


def test_stdio() -> bool:
    venv_python = sys.executable
    verdict = _run_ts(["stdio"], extra_env={"PYTHON": venv_python})
    return verdict is not None and _print_verdict(verdict)


def test_http() -> bool:
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_server.server",
         "--transport", "streamable-http", "--port", str(HTTP_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "MCP_LOG_LEVEL": "WARNING"},
    )
    try:
        if not _wait_for_port(HTTP_PORT, timeout=5.0):
            print(f"FAIL: HTTP server did not bind {HTTP_PORT}")
            return False
        url = f"http://127.0.0.1:{HTTP_PORT}/mcp"
        verdict = _run_ts(["http", url])
        return verdict is not None and _print_verdict(verdict)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(2)


def main() -> int:
    if not shutil.which("npx"):
        print("FAIL: npx not found in PATH; install Node to run TypeScript SDK probe.")
        return 2
    if not _ensure_node_modules():
        return 2

    stdio_ok = test_stdio()
    print()
    http_ok = test_http()
    print()
    if stdio_ok and http_ok:
        print("PASS — TypeScript SDK compatibility verified on stdio and streamable-http")
        return 0
    print("FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
