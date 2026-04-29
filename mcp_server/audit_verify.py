"""Verify the integrity of a JSONL audit log written by the MCP server.

The audit log is a hash chain (schema v2): every record carries `seq`,
`prev_hash`, and `hash` (SHA-256 over the canonical-JSON of the record
minus `hash` itself). Walking the file and checking each link detects
any insertion, deletion, or edit applied after the record was written.

This is *tamper-evidence*, not *forge resistance*: an adversary who
controls the writer process can produce a self-consistent chain of
fabricated records. The chain protects against post-hoc edits — the
common attacker model when the writer is trusted but the storage path
is shared (e.g. a host volume read by a log shipper).

Usage:
  python -m mcp_server.audit_verify <path>

Exit codes:
  0 — chain intact (all records pass)
  1 — chain mismatch (insertion / edit / deletion detected)
  2 — file unreadable, unparseable, or missing chain fields (likely a
       v1 log written before chain landed)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

GENESIS_HASH = "0" * 64


def _canonical_minus_hash(record: dict) -> bytes:
    r = {k: v for k, v in record.items() if k != "hash"}
    return json.dumps(
        r, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")


def verify(path: Path) -> tuple[int, str]:
    """Walk the file and verify the chain. Returns (exit_code, message)."""
    try:
        f = path.open()
    except OSError as e:
        return 2, f"cannot open {path}: {e}"

    expected_seq = 1
    expected_prev = GENESIS_HASH
    count = 0

    with f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue

            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                return 2, f"line {lineno}: invalid JSON: {e}"

            for field in ("seq", "prev_hash", "hash"):
                if field not in rec:
                    return 2, (
                        f"line {lineno}: missing chain field {field!r} "
                        f"(v1 log? — chain not in v2 schema yet)"
                    )

            if rec["seq"] != expected_seq:
                return 1, (
                    f"line {lineno}: seq mismatch "
                    f"(got {rec['seq']!r}, expected {expected_seq})"
                )

            if rec["prev_hash"] != expected_prev:
                return 1, (
                    f"line {lineno}: prev_hash mismatch — record was inserted, "
                    f"deleted, or reordered upstream"
                )

            recomputed = hashlib.sha256(_canonical_minus_hash(rec)).hexdigest()
            if recomputed != rec["hash"]:
                return 1, (
                    f"line {lineno}: hash mismatch — record was edited "
                    f"after write"
                )

            expected_seq = rec["seq"] + 1
            expected_prev = rec["hash"]
            count += 1

    return 0, f"chain intact: {count} record(s) verified"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mcp_server.audit_verify")
    p.add_argument("path", type=Path, help="Path to the JSONL audit log file")
    args = p.parse_args(argv)
    code, msg = verify(args.path)
    if code == 0:
        print(msg)
    else:
        print(msg, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
