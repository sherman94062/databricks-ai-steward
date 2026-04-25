"""Probe B: server lifecycle when the client disconnects mid-call.

Spawns stress.server as a subprocess, hand-crafts the JSON-RPC handshake,
fires a hanging tool, then closes stdin (simulating a client that died
without saying goodbye). Observes whether the server detects EOF and
exits, and how long it takes.

Reports for each hang variant:
  - exit code
  - time from stdin-close to process exit
  - whether SIGKILL was needed

Run:   python -m stress.probe_b_disconnect
"""

from __future__ import annotations

import json
import subprocess
import sys
import time


def _send(proc: subprocess.Popen, msg: dict) -> None:
    line = json.dumps(msg) + "\n"
    proc.stdin.write(line.encode())
    proc.stdin.flush()


def _recv_line(proc: subprocess.Popen, timeout: float = 5.0) -> dict | None:
    """Read one JSON-RPC line from stdout. Returns None on timeout/EOF."""
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            return None
        buf += line
        try:
            return json.loads(buf.decode())
        except json.JSONDecodeError:
            continue
    return None


def _trial(hang_tool: str) -> dict:
    proc = subprocess.Popen(
        [sys.executable, "-m", "stress.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    # Handshake
    _send(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "probe-b", "version": "0"},
        },
    })
    init_resp = _recv_line(proc)
    assert init_resp and init_resp.get("id") == 1, f"init failed: {init_resp}"

    _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    # Fire the hang (we will not wait for response)
    _send(proc, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": hang_tool, "arguments": {}},
    })

    # Give the server a beat to actually start the tool
    time.sleep(0.1)

    # Close our end of stdin — simulate client death
    t0 = time.monotonic()
    proc.stdin.close()

    # Poll for exit, up to 5s
    exited_in: float | None = None
    for _ in range(50):
        if proc.poll() is not None:
            exited_in = time.monotonic() - t0
            break
        time.sleep(0.1)

    needed_sigkill = False
    if exited_in is None:
        proc.kill()
        needed_sigkill = True
        proc.wait(timeout=2.0)
        exited_in = time.monotonic() - t0

    return {
        "hang_tool": hang_tool,
        "exit_code": proc.returncode,
        "seconds_after_stdin_close": round(exited_in, 3),
        "needed_sigkill": needed_sigkill,
    }


def main() -> None:
    print(f"{'tool':<35}{'exit_code':<12}{'exit_after':<14}{'sigkill?'}")
    print("-" * 75)
    for tool in ("hangs_forever_async_guarded", "hangs_forever_guarded"):
        r = _trial(tool)
        print(f"{r['hang_tool']:<35}"
              f"{str(r['exit_code']):<12}"
              f"{str(r['seconds_after_stdin_close']) + 's':<14}"
              f"{r['needed_sigkill']}")


if __name__ == "__main__":
    main()
