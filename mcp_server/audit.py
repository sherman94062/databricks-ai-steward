"""Persistent audit log for the MCP server.

Every tool call produces two JSONL records (start + end) sharing a
`request_id`. Records carry the caller identity (a `contextvars`-scoped
value set by the transport layer), the tool name, latency, outcome,
and response size. Argument *values* are not logged — only argument
*names* and a hash — because tool args may contain SQL fragments,
table names, or other workspace internals that we treat the same way
we treat tokens (cf. `_scrub` in `app.py`).

Configuration:
  * `MCP_AUDIT_LOG_PATH`   — file path; if unset, audit goes to stderr
                             only (still useful for k8s log collectors)
  * `MCP_CALLER_ID`        — default caller identity for this process

The module is import-safe and side-effect free at import time. Any
operator that wants to inspect recent records can `tail -f` the JSONL
file or pipe it into Splunk / Datadog / wherever.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_log = logging.getLogger("mcp_server.audit")

# ---- Hash-chain (tamper-evidence) -------------------------------------
# Every record gets `seq` (monotonic) + `prev_hash` (previous record's
# hash) + `hash` (SHA-256 over the canonical-JSON of this record minus
# the `hash` field itself). A verifier walking the file can detect any
# insert / edit / delete after the fact. No keys, no signing — just
# collision-resistance. Forge resistance is *not* a goal; for that, pair
# with HMAC and a key the steward never sees client-side.
#
# Schema is v2 (see AUDIT_LOG_SCHEMA.md). Previously-written v1 logs
# (no chain fields) won't verify under this CLI — they remain readable,
# they're just not tamper-evident.

GENESIS_HASH = "0" * 64

_chain_lock = threading.Lock()
_seq = 0
_last_hash = GENESIS_HASH
_chain_initialized = False


def _canonical(record: dict) -> bytes:
    return json.dumps(
        record, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")


def _recover_chain_from_file(path: Path) -> None:
    """Read the existing audit file (if any) and resume the chain from
    its tail. Lets a restarted process continue an unbroken chain
    instead of resetting to genesis (which would be a verifier-detectable
    discontinuity)."""
    global _seq, _last_hash
    try:
        if not path.exists() or path.stat().st_size == 0:
            return
        last_seq = 0
        last_hash = GENESIS_HASH
        with path.open() as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "seq" in rec and "hash" in rec:
                    last_seq = rec["seq"]
                    last_hash = rec["hash"]
        _seq = last_seq
        _last_hash = last_hash
    except OSError:
        # If the file is unreadable we'd rather start a new chain than
        # crash the server. The verifier will surface the discontinuity.
        pass


def _ensure_chain_initialized() -> None:
    global _chain_initialized
    if _chain_initialized:
        return
    path = os.environ.get("MCP_AUDIT_LOG_PATH", "").strip()
    if path:
        _recover_chain_from_file(Path(path).expanduser())
    _chain_initialized = True


def _link_record(record: dict) -> None:
    """Mutate `record` in place: assign seq, prev_hash, hash. Caller
    must hold `_chain_lock`."""
    global _seq, _last_hash
    _seq += 1
    record["seq"] = _seq
    record["prev_hash"] = _last_hash
    record["hash"] = hashlib.sha256(_canonical(record)).hexdigest()
    _last_hash = record["hash"]

# Caller identity for the *current* in-flight request. Transport layers
# (stdio entry, bearer-auth middleware) overwrite this per-call; tools
# read it via `current_caller_id`. The contextvar itself defaults to
# None to satisfy ruff B039 (mutable default); `current_caller_id`
# substitutes the env-derived process default at read time.
_caller_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mcp_caller_id", default=None,
)


def _process_default_caller() -> str:
    return os.environ.get("MCP_CALLER_ID", "unknown")


def set_caller_id(caller: str) -> contextvars.Token:
    """Set the caller identity for the current async context.
    Returns a token the caller can pass to `reset_caller_id` to undo
    the change (Starlette middleware uses this pattern)."""
    return _caller_id.set(caller or "unknown")


def reset_caller_id(token: contextvars.Token) -> None:
    _caller_id.reset(token)


def current_caller_id() -> str:
    return _caller_id.get() or _process_default_caller()


# The currently-executing tool, set by `_guard` on entry. Used by
# `databricks.client.get_workspace()` to pick a per-tool credential
# when one is configured (least-privilege downstream of the steward).
_current_tool: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mcp_current_tool", default=None,
)


def set_current_tool(name: str) -> contextvars.Token:
    return _current_tool.set(name)


def reset_current_tool(token: contextvars.Token) -> None:
    _current_tool.reset(token)


def current_tool() -> str | None:
    return _current_tool.get()


# The MCP request ID for the currently-executing tool call. Set by
# `_guard`; backend operations (e.g. Databricks statement execution)
# read it so they can attribute correlation events back to the
# originating MCP request without plumbing it through every call.
_current_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mcp_current_request_id", default=None,
)


def set_current_request_id(rid: str) -> contextvars.Token:
    return _current_request_id.set(rid)


def reset_current_request_id(token: contextvars.Token) -> None:
    _current_request_id.reset(token)


def current_request_id() -> str | None:
    return _current_request_id.get()


def new_request_id() -> str:
    return uuid.uuid4().hex


def _arg_names_only(args: tuple, kwargs: dict) -> dict:
    """Capture argument *shape* (names + types + a digest), not values.

    Logging values would leak SQL bodies, table FQNs, etc. through the
    audit channel. The hash lets correlation queries identify "the same
    call repeated 500 times" without exposing what was actually sent.
    """
    if args or kwargs:
        try:
            payload = json.dumps(
                {"args": list(args), "kwargs": kwargs},
                default=str, sort_keys=True,
            )
            digest = hashlib.sha256(payload.encode()).hexdigest()[:12]
        except Exception:
            digest = "unhashable"
    else:
        digest = "no-args"
    return {
        "kw_names": sorted(kwargs.keys()),
        "pos_count": len(args),
        "args_digest": digest,
    }


# Tests set this to a list to capture audit records without spamming
# stderr; production leaves it as None and records go to file + stderr.
_capture: list[dict] | None = None


def _emit(record: dict) -> None:
    """Append one record to the audit log file (if configured) and stderr."""
    record.setdefault("ts", round(time.time(), 3))
    record.setdefault("caller_id", current_caller_id())

    # Capture mode is test-only. Skip chain linking so existing tests
    # aren't sensitive to chain state leaking across the test session.
    # Tests that need to exercise the chain use a real file path.
    if _capture is not None:
        _capture.append(record)
        return

    # Hold the chain lock through the file write so that physical file
    # order matches `seq` order even under concurrent emits. (The whole
    # path is sync; awaiting inside the lock isn't a concern.)
    with _chain_lock:
        _ensure_chain_initialized()
        _link_record(record)
        line = json.dumps(record, default=str)

        path = os.environ.get("MCP_AUDIT_LOG_PATH", "").strip()
        if path:
            try:
                p = Path(path).expanduser()
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a") as f:
                    f.write(line + "\n")
            except OSError as e:
                # Don't let audit-log write failures break the server. Surface
                # via a stderr warning so operators see the problem.
                _log.warning("audit log write failed: %s", e)

        # Always emit to stderr at INFO so logs collectors capture audit
        # even when the file path isn't configured. Suppress when
        # MCP_AUDIT_DISABLE_STDERR=1 (useful in noisy CI / log shippers
        # that already pull from the file).
        if os.environ.get("MCP_AUDIT_DISABLE_STDERR", "").strip() not in ("1", "true", "yes"):
            print(line, file=sys.stderr, flush=True)


class capture:
    """Context manager that collects audit records into a list. Tests use
    `with audit.capture() as records: ...` and assert against records."""

    def __enter__(self) -> list[dict]:
        global _capture
        self._records: list[dict] = []
        self._prev = _capture
        _capture = self._records
        return self._records

    def __exit__(self, *exc) -> None:
        global _capture
        _capture = self._prev


def emit_tool_start(
    tool: str, request_id: str, args: tuple, kwargs: dict,
) -> None:
    _emit({
        "event": "tool.start",
        "request_id": request_id,
        "tool": tool,
        **_arg_names_only(args, kwargs),
    })


def emit_tool_end(
    tool: str, request_id: str,
    latency_ms: float,
    outcome: str,
    error_type: str | None = None,
    response_bytes: int | None = None,
) -> None:
    rec: dict[str, Any] = {
        "event": "tool.end",
        "request_id": request_id,
        "tool": tool,
        "latency_ms": round(latency_ms, 2),
        "outcome": outcome,
    }
    if error_type:
        rec["error_type"] = error_type
    if response_bytes is not None:
        rec["response_bytes"] = response_bytes
    _emit(rec)


def emit_rate_limit_exceeded(
    tool: str, request_id: str, limit: int, window_s: int,
) -> None:
    _emit({
        "event": "tool.rate_limit_exceeded",
        "request_id": request_id,
        "tool": tool,
        "limit": limit,
        "window_s": window_s,
    })


def emit_databricks_statement(
    request_id: str | None,
    statement_id: str | None,
    warehouse_id: str | None = None,
    state: str | None = None,
    tool: str | None = None,
) -> None:
    """Correlation event linking an MCP `request_id` to a Databricks
    `statement_id`. Emitted from `sql_tools._execute_with_cancellation`
    after the SDK's `execute_statement` returns. Operators tracing a
    request through the system follow the chain:

      audit `tool.start` → audit `tool.databricks_statement` →
      Databricks `system.query.history` (filter by `statement_id`) →
      original SQL text + execution plan
    """
    _emit({
        "event": "tool.databricks_statement",
        "request_id": request_id,
        "statement_id": statement_id,
        "warehouse_id": warehouse_id,
        "state": state,
        "tool": tool or current_tool(),
    })
