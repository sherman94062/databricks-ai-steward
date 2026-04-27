"""Probe: token never appears in any output channel.

Two complementary checks:

1. **Static** — grep the codebase for any logger or formatter call that
   includes a token-shaped variable. Catches code paths the runtime
   probe might miss.

2. **Runtime** — set DATABRICKS_TOKEN and MCP_BEARER_TOKEN to known
   sentinel values, run a session that intentionally hits the error
   path (bad workspace URL forces an SDK exception that may include
   the token in its message), capture *all* stdout + stderr + the
   tool response, and assert that neither sentinel appears literally.

Run:   python -m stress.probe_token_audit
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SENTINEL_TOKEN = "dapiTOKEN_SENTINEL_must_not_leak_2026_xyzpdq"
SENTINEL_BEARER = "BEARER_SENTINEL_must_not_leak_2026_xyzpdq"


def static_grep() -> tuple[bool, list[str]]:
    """Look for the narrow anti-pattern: a `log.X(...)`, `print(...)`,
    or `f"..."` that interpolates a variable named *token (lowercase),
    in production code (not tests/probes).

    A token's *value* is what we care about. The env-var *name*
    (`DATABRICKS_TOKEN`, `MCP_BEARER_TOKEN`) appearing in code is fine —
    that's just configuration plumbing. The runtime check handles the
    rest by exercising the actual error paths with a sentinel."""
    suspicious: list[str] = []

    # Match: log.X("...{token}..."), print(... token ...), or
    # `return f"...{token}..."` where `token` is a lowercase identifier.
    output_pattern = re.compile(
        r"(?:\blog\.[a-z]+\(|\bprint\(|\braise\s+\w+\(|\breturn\s+f?[\"']).*"
        r"\{[^}]*\btoken\b[^}]*\}",
        re.IGNORECASE,
    )

    skip_dirs = {".venv", "node_modules", ".pytest_cache", "tests", "stress"}

    for path in REPO.rglob("*.py"):
        if any(seg in skip_dirs for seg in path.parts):
            continue
        try:
            for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if not output_pattern.search(line):
                    continue
                # Allowlist known-safe output that interpolates len(token).
                if "len(token)" in line or "<redacted" in line:
                    continue
                suspicious.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()[:120]}")
        except OSError:
            pass
    return (len(suspicious) == 0), suspicious


def runtime_check() -> tuple[bool, str]:
    """Force an SDK error path with a sentinel token; assert no leak."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_server.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "MCP_LOG_LEVEL": "DEBUG",   # most verbose to catch any leak
            "DATABRICKS_TOKEN": SENTINEL_TOKEN,
            # Use a non-routable host so the SDK fails fast with a connection error.
            "DATABRICKS_HOST": "https://this-host-does-not-exist.invalid",
            "MCP_BEARER_TOKEN": SENTINEL_BEARER,
        },
    )

    def send(msg):
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        proc.stdin.flush()

    send({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "probe-token-audit", "version": "0"},
        },
    })
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "list_catalogs", "arguments": {}},
    })

    # Read both responses (init + tool call), then close stdin.
    captured = b""
    for _ in range(2):
        line = proc.stdout.readline()
        if not line:
            break
        captured += line

    proc.stdin.close()
    try:
        rest_stdout, stderr = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        rest_stdout, stderr = proc.communicate()

    full = (captured + rest_stdout + stderr).decode(errors="replace")

    leaks = []
    if SENTINEL_TOKEN in full:
        idx = full.index(SENTINEL_TOKEN)
        leaks.append(f"DATABRICKS_TOKEN visible near: ...{full[max(0,idx-80):idx+80]!r}...")
    if SENTINEL_BEARER in full:
        idx = full.index(SENTINEL_BEARER)
        leaks.append(f"MCP_BEARER_TOKEN visible near: ...{full[max(0,idx-80):idx+80]!r}...")

    if not leaks:
        return True, "no sentinel substring found in stdout/stderr/response"
    return False, "\n  ".join(leaks)


def main() -> int:
    print("[static] scanning Python sources for token-shaped log/format calls...")
    static_ok, suspicious = static_grep()
    if static_ok:
        print("  ✓ no suspicious patterns")
    else:
        print(f"  ✗ {len(suspicious)} suspicious line(s):")
        for s in suspicious[:20]:
            print(f"    {s}")

    print()
    print("[runtime] sentinel-token error-path test...")
    runtime_ok, detail = runtime_check()
    print(f"  {'✓' if runtime_ok else '✗'} {detail}")

    print()
    if static_ok and runtime_ok:
        print("PASS — token never visible in any captured output")
        return 0
    print("FAIL — token leak surface found")
    return 1


if __name__ == "__main__":
    sys.exit(main())
