"""Probe C: server response to SIGINT / SIGTERM during in-flight tool call.

Same setup as probe_b but instead of closing stdin we send a signal and
observe shutdown behavior.

Run:   python -m stress.probe_c_signal
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time


def _send(proc: subprocess.Popen, msg: dict) -> None:
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    proc.stdin.flush()


def _recv_line(proc: subprocess.Popen) -> dict | None:
    line = proc.stdout.readline()
    if not line:
        return None
    return json.loads(line.decode())


def _trial(hang_tool: str, sig: int) -> dict:
    proc = subprocess.Popen(
        [sys.executable, "-m", "stress.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    _send(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "probe-c", "version": "0"},
        },
    })
    init_resp = _recv_line(proc)
    assert init_resp and init_resp.get("id") == 1, f"init failed: {init_resp}"

    _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    _send(proc, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": hang_tool, "arguments": {}},
    })

    time.sleep(0.1)

    t0 = time.monotonic()
    proc.send_signal(sig)

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
        "signal": signal.Signals(sig).name,
        "exit_code": proc.returncode,
        "seconds_after_signal": round(exited_in, 3),
        "needed_sigkill": needed_sigkill,
    }


def main() -> None:
    print(f"{'tool':<35}{'signal':<10}{'exit_code':<12}{'exit_after':<14}{'sigkill?'}")
    print("-" * 85)
    for sig in (signal.SIGINT, signal.SIGTERM):
        for tool in ("hangs_forever_async_guarded", "hangs_forever_guarded"):
            r = _trial(tool, sig)
            print(f"{r['hang_tool']:<35}"
                  f"{r['signal']:<10}"
                  f"{str(r['exit_code']):<12}"
                  f"{str(r['seconds_after_signal']) + 's':<14}"
                  f"{r['needed_sigkill']}")


if __name__ == "__main__":
    main()
