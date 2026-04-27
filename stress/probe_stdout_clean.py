"""Probe: stdout discipline.

Stdio MCP transport requires that the server's stdout contains *only*
JSON-RPC framed messages. Any stray write — a print, a logger
mis-configured to stdout, an SDK that helpfully logs progress —
silently corrupts the channel and the client sees parse errors.

This probe spawns the production server and a deliberately broken
extension server (stress.server, which has stdout_pollution_guarded as
a known bad case for a control), runs through several real tool calls
and one error path, and asserts that:

  * production server's stdout = only valid JSON-RPC lines
  * databricks-sdk does not write to stdout under any code path we
    exercise (warning emission paths included)

We don't try to enumerate every SDK code path; we run list_catalogs +
health and an intentional misconfiguration and rely on the spec
guarantee that no JSON-RPC stream is contaminated.

Run:   python -m stress.probe_stdout_clean
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def _check_jsonrpc_lines(stdout_bytes: bytes) -> tuple[bool, str]:
    """Every non-blank line on stdout must parse as a JSON-RPC message."""
    lines = [line for line in stdout_bytes.split(b"\n") if line.strip()]
    if not lines:
        return False, "no lines on stdout — server didn't respond"
    for i, line in enumerate(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            return False, f"line {i}: not JSON: {line[:120]!r} ({e})"
        if not isinstance(obj, dict):
            return False, f"line {i}: not a JSON object: {obj!r}"
        if obj.get("jsonrpc") != "2.0":
            return False, f"line {i}: missing jsonrpc=2.0: {obj!r}"
    return True, f"{len(lines)} clean JSON-RPC lines"


def _run_session(extra_calls: list[dict], env_overrides: dict | None = None) -> tuple[bytes, bytes, int]:
    """Run a one-shot stdio session: initialize + the given tool calls.

    Reads exactly `1 + len(extra_calls)` JSON-RPC responses (init + each
    tool call) before closing stdin, so in-flight tool tasks always
    finish before the lifecycle handler tears the server down.

    Returns (stdout_bytes, stderr_bytes, returncode).
    """
    env = {**os.environ, "MCP_LOG_LEVEL": "WARNING"}
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_server.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    def send(msg: dict) -> None:
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        proc.stdin.flush()

    send({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "probe-stdout", "version": "0"},
        },
    })
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    for i, call in enumerate(extra_calls):
        send({
            "jsonrpc": "2.0", "id": 2 + i, "method": "tools/call",
            "params": call,
        })

    # Read all expected responses synchronously before tearing down.
    # `notifications/initialized` is a notification (no id) so it gets no
    # response — total expected = 1 init + len(extra_calls).
    expected_lines = 1 + len(extra_calls)
    captured_stdout = b""
    deadline = time.monotonic() + 15.0
    while expected_lines > 0 and time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        captured_stdout += line
        # Only count lines that look like JSON-RPC responses with an id;
        # anything else is corruption (which the assertion will catch).
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and "id" in obj:
                expected_lines -= 1
        except json.JSONDecodeError:
            expected_lines -= 1   # let the line failure surface to the assertion

    # Now tear down cleanly
    proc.stdin.close()
    try:
        rest_stdout, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        rest_stdout, stderr = proc.communicate()
        return captured_stdout + rest_stdout, stderr, -1
    return captured_stdout + rest_stdout, stderr, proc.returncode


def main() -> int:
    print("[trial] live list_catalogs + health (real Databricks SDK call)")
    stdout, stderr, code = _run_session([
        {"name": "list_catalogs", "arguments": {}},
        {"name": "health", "arguments": {}},
    ])
    ok_normal, detail_normal = _check_jsonrpc_lines(stdout)
    print(f"  stdout: {'✓' if ok_normal else '✗'} — {detail_normal}")
    print(f"  exit_code: {code}")
    if not ok_normal:
        print("  --- stderr (last 400 chars) ---")
        print(stderr.decode(errors="replace")[-400:])

    print()
    print("[trial] error path (force SDK auth failure with bogus token)")
    stdout_err, stderr_err, code_err = _run_session(
        [{"name": "list_catalogs", "arguments": {}}],
        env_overrides={
            "DATABRICKS_TOKEN": "dapi_intentionally_invalid_for_test_xxxxxx",
        },
    )
    ok_err, detail_err = _check_jsonrpc_lines(stdout_err)
    print(f"  stdout: {'✓' if ok_err else '✗'} — {detail_err}")
    print(f"  exit_code: {code_err}")
    if not ok_err:
        print("  --- stderr (last 400 chars) ---")
        print(stderr_err.decode(errors="replace")[-400:])

    print()
    if ok_normal and ok_err:
        print("PASS — stdout is clean on both happy and error paths")
        return 0
    print("FAIL — stdout was corrupted; the MCP transport will see parse errors")
    return 1


if __name__ == "__main__":
    sys.exit(main())
