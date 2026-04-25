"""Probe: graceful restart contract.

Spawns stress.server, kicks off N in-flight async hang calls, sends SIGTERM,
and asserts:
  * process exits with code 0 (clean shutdown, not killed)
  * exit happens within MCP_SHUTDOWN_GRACE_S + small buffer
  * 'graceful shutdown' was logged
  * the registered cleanup callback ran (STRESS_CLEANUP_RAN marker)

Compare to probe_c, which showed the pre-lifecycle behavior:
SIGTERM exited the process with code -15 (killed by signal) in 0.1s,
no cleanup, no graceful path.

Run:   python -m stress.probe_restart
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time


GRACE_S = 3.0   # tighter than the default 5s for faster probing
MAX_WAIT_S = 6.0  # allow some buffer past grace


def _send(proc: subprocess.Popen, msg: dict) -> None:
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    proc.stdin.flush()


def _recv(proc: subprocess.Popen) -> dict | None:
    line = proc.stdout.readline()
    if not line:
        return None
    return json.loads(line.decode())


def main() -> int:
    env = {**os.environ, "MCP_SHUTDOWN_GRACE_S": str(GRACE_S)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "stress.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    _send(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "probe-restart", "version": "0"},
        },
    })
    init_resp = _recv(proc)
    assert init_resp and init_resp.get("id") == 1, f"init failed: {init_resp}"
    _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    # Fire 5 async hangs (no timeout, just sleep 300s)
    n_in_flight = 5
    for i in range(n_in_flight):
        _send(proc, {
            "jsonrpc": "2.0", "id": 100 + i, "method": "tools/call",
            "params": {"name": "hangs_forever_async_guarded", "arguments": {}},
        })

    # Give them a beat to actually start
    time.sleep(0.3)

    print(f"[step] sending SIGTERM with {n_in_flight} async hangs in flight, "
          f"grace={GRACE_S}s", file=sys.stderr)
    t0 = time.monotonic()
    proc.send_signal(signal.SIGTERM)

    # Wait for exit
    exit_code: int | None = None
    deadline = t0 + MAX_WAIT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            exit_code = proc.returncode
            break
        time.sleep(0.05)

    elapsed = time.monotonic() - t0

    if exit_code is None:
        proc.kill()
        proc.wait(2)
        # Read whatever stderr we got before SIGKILL — invaluable for diagnosing.
        stderr = b""
        try:
            stderr = proc.stderr.read()
        except Exception:
            pass
        print(f"FAIL: did not exit within {MAX_WAIT_S}s; SIGKILL'd")
        print()
        print("--- captured stderr ---")
        print(stderr.decode(errors="replace") or "(empty)")
        return 1

    stderr = b""
    try:
        stderr = proc.stderr.read()
    except Exception:
        pass
    stderr_text = stderr.decode(errors="replace")

    graceful_log_seen = "graceful shutdown" in stderr_text
    cleanup_ran = "STRESS_CLEANUP_RAN" in stderr_text
    cancelled_log = "server cancelled cleanly" in stderr_text or "stopped cleanly" in stderr_text

    print()
    print(f"exit_code         {exit_code}")
    print(f"elapsed           {elapsed:.2f}s  (grace={GRACE_S}s)")
    print(f"graceful log      {'✓' if graceful_log_seen else '✗ MISSING'}")
    print(f"server cancelled  {'✓' if cancelled_log else '✗ MISSING'}")
    print(f"cleanup ran       {'✓' if cleanup_ran else '✗ MISSING'}")

    passed = (
        exit_code == 0
        and elapsed <= GRACE_S + 1.0
        and graceful_log_seen
        and cleanup_ran
    )
    print()
    print("PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
