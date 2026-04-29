"""Tests for the audit-log hash chain (tamper-evidence).

The chain is the v2 audit-log integrity story: every record carries
`seq`, `prev_hash`, and `hash`. A verifier walking the file detects
any insertion / edit / deletion applied after-the-fact. These tests
exercise the chain through the real `_emit` path (file mode) rather
than `audit.capture()` because capture mode skips chain linking.
"""

from __future__ import annotations

import json

import pytest

from mcp_server import audit
from mcp_server.audit_verify import GENESIS_HASH, verify


@pytest.fixture
def audit_path(tmp_path, monkeypatch):
    """Fresh chain state + a clean audit file path. The reset is what
    makes per-test chain assertions reproducible — module state would
    otherwise leak across tests."""
    p = tmp_path / "audit.jsonl"
    monkeypatch.setenv("MCP_AUDIT_LOG_PATH", str(p))
    monkeypatch.setenv("MCP_AUDIT_DISABLE_STDERR", "1")
    monkeypatch.setattr(audit, "_seq", 0)
    monkeypatch.setattr(audit, "_last_hash", audit.GENESIS_HASH)
    monkeypatch.setattr(audit, "_chain_initialized", False)
    return p


def _read_records(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_first_record_has_genesis_prev_hash(audit_path):
    audit.emit_tool_start("foo", "rid1", (), {})
    [rec] = _read_records(audit_path)
    assert rec["seq"] == 1
    assert rec["prev_hash"] == GENESIS_HASH
    assert len(rec["hash"]) == 64


def test_subsequent_records_chain_to_previous_hash(audit_path):
    audit.emit_tool_start("foo", "rid1", (), {})
    audit.emit_tool_end("foo", "rid1", 1.0, "success")
    audit.emit_tool_start("bar", "rid2", (), {})
    r1, r2, r3 = _read_records(audit_path)
    assert r1["seq"] == 1 and r2["seq"] == 2 and r3["seq"] == 3
    assert r2["prev_hash"] == r1["hash"]
    assert r3["prev_hash"] == r2["hash"]


def test_verifier_passes_clean_chain(audit_path):
    audit.emit_tool_start("foo", "rid1", (), {})
    audit.emit_tool_end("foo", "rid1", 1.0, "success")
    audit.emit_tool_start("bar", "rid2", (), {"x": 1})
    audit.emit_tool_end("bar", "rid2", 2.0, "success")
    code, msg = verify(audit_path)
    assert code == 0, msg
    assert "4 record" in msg


def test_verifier_detects_record_edit(audit_path):
    """Tamper with a record's payload while keeping its chain fields —
    the recomputed hash will not match the stored hash."""
    audit.emit_tool_start("foo", "rid1", (), {})
    audit.emit_tool_end("foo", "rid1", 1.0, "success")

    lines = audit_path.read_text().splitlines()
    rec = json.loads(lines[0])
    rec["tool"] = "evil"  # adversary tries to rewrite history
    lines[0] = json.dumps(rec)
    audit_path.write_text("\n".join(lines) + "\n")

    code, msg = verify(audit_path)
    assert code == 1
    assert "hash mismatch" in msg


def test_verifier_detects_deleted_record(audit_path):
    audit.emit_tool_start("foo", "rid1", (), {})
    audit.emit_tool_end("foo", "rid1", 1.0, "success")
    audit.emit_tool_start("bar", "rid2", (), {})

    lines = audit_path.read_text().splitlines()
    audit_path.write_text(lines[0] + "\n" + lines[2] + "\n")  # drop middle

    code, msg = verify(audit_path)
    assert code == 1
    # Could surface as either seq mismatch or prev_hash mismatch.
    assert "mismatch" in msg


def test_verifier_detects_inserted_record(audit_path):
    audit.emit_tool_start("foo", "rid1", (), {})

    forged = {
        "event": "tool.start",
        "tool": "evil",
        "request_id": "fake",
        "ts": 1.0,
        "caller_id": "x",
        "kw_names": [],
        "pos_count": 0,
        "args_digest": "no-args",
        "seq": 2,
        "prev_hash": "0" * 64,
        "hash": "f" * 64,
    }
    with audit_path.open("a") as f:
        f.write(json.dumps(forged) + "\n")

    code, msg = verify(audit_path)
    assert code == 1


def test_verifier_rejects_v1_log_without_chain_fields(tmp_path):
    """A v1 log (no chain fields) shouldn't silently pass — it's
    legible but not tamper-evident, and the verifier has to flag that
    so operators don't get a false sense of security."""
    p = tmp_path / "v1.jsonl"
    p.write_text(
        json.dumps({"event": "tool.start", "tool": "foo", "ts": 1.0,
                    "request_id": "rid1", "caller_id": "x"}) + "\n",
    )
    code, msg = verify(p)
    assert code == 2
    assert "v1" in msg or "chain field" in msg


def test_chain_continues_across_simulated_restart(audit_path, monkeypatch):
    """A restarted process that re-opens the same file must continue
    the chain rather than reset to genesis (which a verifier would
    flag as a discontinuity)."""
    audit.emit_tool_start("foo", "rid1", (), {})
    audit.emit_tool_end("foo", "rid1", 1.0, "success")

    # Simulate restart: clear in-memory chain state, leave file.
    monkeypatch.setattr(audit, "_seq", 0)
    monkeypatch.setattr(audit, "_last_hash", audit.GENESIS_HASH)
    monkeypatch.setattr(audit, "_chain_initialized", False)

    audit.emit_tool_start("bar", "rid2", (), {})
    audit.emit_tool_end("bar", "rid2", 2.0, "success")

    code, msg = verify(audit_path)
    assert code == 0, msg

    records = _read_records(audit_path)
    assert [r["seq"] for r in records] == [1, 2, 3, 4]
    assert records[2]["prev_hash"] == records[1]["hash"]


def test_emitted_record_does_not_leak_arg_values_through_hash(audit_path):
    """The chain hash is over the record's canonical form, which
    includes args_digest (already a SHA-256 prefix) but never raw arg
    values. Sanity-check that no sentinel string appears anywhere in
    the on-disk record — including the hash hex."""
    sentinel = "dapiSENTINEL_TOKEN_MUST_NOT_LEAK"
    audit.emit_tool_start("secret_tool", "rid1", (), {"api_token": sentinel})
    text = audit_path.read_text()
    assert sentinel not in text
