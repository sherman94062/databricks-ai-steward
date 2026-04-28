# databricks-ai-steward — Audit Log Schema

The audit log is the canonical answer to *"what did this server do?"*
This document is the schema reference: every event type, every field,
the immutability and retention model, and the deliberate decisions
about what is not logged.

The audit log is one of three observability signals (the others are
Prometheus metrics at `/metrics` and OpenTelemetry traces via
`OTEL_EXPORTER_OTLP_ENDPOINT`). All three share the same
`request_id` so an operator tracing a single tool call can pivot
between them without manual correlation. See
[`RUNBOOK.md`](RUNBOOK.md) "Tracing a request through the system" for
the recipe.

---

## Format

JSON Lines (RFC 7464). One self-contained JSON object per line, no
trailing comma, newline-terminated. UTF-8.

A single tool call produces between two and four records, in this
order:

1. `tool.start` — entry into `_guard`, before any check
2. `tool.rate_limit_exceeded` — *only if* the rate limiter rejects
3. `tool.databricks_statement` — *only for* tools that run a SQL
   statement (the SQL tool family)
4. `tool.end` — exit from `_guard`, with outcome

Records share a `request_id` (UUID hex, no dashes) so they can be
correlated even when interleaved with other concurrent calls.

---

## Common fields

Every record carries these:

| Field | Type | Description |
|---|---|---|
| `event` | string | Event type — one of `tool.start`, `tool.end`, `tool.rate_limit_exceeded`, `tool.databricks_statement` |
| `ts` | float | Unix epoch seconds, three decimals (millisecond resolution) |
| `request_id` | string \| null | UUID hex, identifies one tool call. Null for events not associated with a single call (none today) |
| `caller_id` | string | Identity of the caller. From `MCP_BEARER_TOKEN_NAME` for HTTP requests, from `MCP_CALLER_ID` for stdio, or `"unknown"` if neither is set |
| `tool` | string | Tool name (matches `tools/list` registration) |

---

## `tool.start`

Emitted at entry into `_guard`, before rate-limit check, before tool
body execution.

| Field | Type | Description |
|---|---|---|
| `event` | string | `"tool.start"` |
| `kw_names` | list[string] | Sorted list of *names* of keyword arguments passed (e.g. `["limit", "since_hours"]`). **Values are deliberately not logged** — see "What is not logged" below |
| `pos_count` | int | Count of positional arguments |
| `args_digest` | string | First 12 hex chars of SHA-256 of the JSON-serialised args. Used to detect repeated calls (same args → same digest) without exposing values |

Example:
```json
{"event":"tool.start","ts":1714329600.123,"request_id":"a1b2c3d4...","caller_id":"team-data","tool":"execute_sql_safe","kw_names":[],"pos_count":1,"args_digest":"3c92e8a5b104"}
```

---

## `tool.rate_limit_exceeded`

Emitted *only* when the rate limiter rejects a call. The corresponding
`tool.end` will follow with `outcome: "rate_limited"`.

| Field | Type | Description |
|---|---|---|
| `event` | string | `"tool.rate_limit_exceeded"` |
| `limit` | int | The cap that was exceeded (per `MCP_RATE_LIMIT` config) |
| `window_s` | int | The window length in seconds for that cap |

Example:
```json
{"event":"tool.rate_limit_exceeded","ts":1714329601.456,"request_id":"a1b2c3d4...","caller_id":"team-data","tool":"execute_sql_safe","limit":5,"window_s":60}
```

---

## `tool.databricks_statement`

Emitted by tools that execute a SQL statement at Databricks
(`execute_sql_safe` and the system-table wrappers that route through
it). Carries the workspace-side identifier so an operator can pivot
from our audit log to `system.query.history`.

| Field | Type | Description |
|---|---|---|
| `event` | string | `"tool.databricks_statement"` |
| `statement_id` | string \| null | Databricks SDK statement ID. Null only on the rare case where the SDK didn't return one (defensive — log a warning if seen) |
| `warehouse_id` | string \| null | Resolved SQL warehouse ID |
| `state` | string \| null | Final terminal state from `StatementState` enum: `SUCCEEDED`, `FAILED`, `CANCELED`, `CLOSED`, etc. |

Example:
```json
{"event":"tool.databricks_statement","ts":1714329602.789,"request_id":"a1b2c3d4...","caller_id":"team-data","tool":"execute_sql_safe","statement_id":"01f1abc...","warehouse_id":"292ff1b9adb98366","state":"SUCCEEDED"}
```

**Operator pivot path**: take the `statement_id` and run

```sql
SELECT statement_text, execution_status, total_duration_ms,
       read_rows, error_message
FROM system.query.history
WHERE statement_id = '<statement_id>'
```

You get the original SQL, plan, and runtime stats — full forensic
detail without needing to log the SQL text in our own audit channel.

---

## `tool.end`

Emitted on every exit path from `_guard`. Always paired with a
`tool.start` having the same `request_id`.

| Field | Type | Description |
|---|---|---|
| `event` | string | `"tool.end"` |
| `latency_ms` | float | Wall-clock duration from `tool.start` to here, milliseconds, two decimals |
| `outcome` | string | One of `"success"`, `"error"`, `"rate_limited"`, `"timeout"` |
| `error_type` | string \| null | Python exception class name when `outcome != "success"`. e.g. `"PermissionDenied"`, `"RateLimitExceeded"`, `"ToolTimeout"`, `"SqlNotAllowed"` |
| `response_bytes` | int \| null | Length of the JSON-encoded response. Best-effort — null on the rare case where the (already-capped) response can't be re-serialised |

Example (success):
```json
{"event":"tool.end","ts":1714329602.999,"request_id":"a1b2c3d4...","caller_id":"team-data","tool":"execute_sql_safe","latency_ms":2876.21,"outcome":"success","response_bytes":1843}
```

Example (rate-limited — pairs with the `tool.rate_limit_exceeded` above):
```json
{"event":"tool.end","ts":1714329601.457,"request_id":"a1b2c3d4...","caller_id":"team-data","tool":"execute_sql_safe","latency_ms":0.34,"outcome":"rate_limited","error_type":"RateLimitExceeded"}
```

---

## What is *not* logged, and why

The audit log records argument *names* and a *digest* but not argument
*values*. This is a deliberate decision, not an oversight:

- **SQL bodies** can contain table FQNs that themselves identify
  workspace internals.
- **Caller-supplied filter strings** can contain PII when the agent
  is acting on user input.
- **Deeply nested args** can balloon the audit-log size without
  proportionate forensic value.
- **Re-emitting the values** through our log channel creates a second
  copy of sensitive data that has to be secured the same way the
  primary copy is.

The mitigation is the `args_digest` — same call = same digest. An
operator can identify "the same SQL was run 500 times by caller X"
without ever needing to see the SQL text. For full forensic replay,
the operator follows the `request_id → statement_id → system.query.history`
chain documented above.

If a deployment requires fuller capture, the recommended path is a
log-shipper-side enrichment that joins `tool.databricks_statement`
records with the matching `system.query.history.statement_text`. That
keeps sensitive payloads in Databricks (where workspace ACLs apply)
rather than duplicating them into the steward's audit channel.

---

## Immutability

The steward writes JSONL records via plain `open(path, "a")` —
**append-only at the application layer, but not cryptographically
tamper-evident**. The immutability guarantee, where it exists, is
provided by the deployment-layer log shipper:

| Storage backend | Immutability mechanism |
|---|---|
| AWS S3 + Object Lock | Compliance mode + retention period |
| GCS + Object Lifecycle / Bucket Lock | Retention policy with locked configuration |
| Azure Blob + Immutability policies | Time-based retention or legal hold |
| Splunk / Datadog / Elastic | Append-only indices + role-restricted edit |
| On-disk (k8s Persistent Volume) | None — operator must rotate and ship to immutable storage |

For a fintech deployment, the recommended pattern is:

1. Steward writes to a host-mounted volume (the JSONL file).
2. A log-shipper sidecar (Vector, Filebeat, Fluent Bit) tails the file
   and writes to S3 + Object Lock (or equivalent).
3. The on-disk file is rotated and deleted on a short cycle (24 h
   typical) — the immutable copy lives in object storage.

The steward does *not* compute hash chains or sign records. That's a
deliberate scope decision: the cost of a robust crypto chain (key
management, verification tooling, recovery on key loss) is
disproportionate to the marginal benefit when the log-shipper-side
storage already provides immutability. If a deployment needs in-app
crypto chaining, it's a custom integration not a built-in feature.

---

## Retention

The steward does not retain audit records itself — it emits and
forgets. Retention is set by the log-shipper / object-storage tier:

- **Recommended minimum** for a regulated workload: 1 year.
- **Recommended for SOC 2 / ISO 27001 audit periods**: 1 year + the
  audit period (typically 12 months).
- **Recommended for forensics**: 7 years on cold storage.

If `MCP_AUDIT_LOG_PATH` is unset, records go only to stderr, which a
container runtime typically retains for hours-to-days. **Production
deployments must set `MCP_AUDIT_LOG_PATH`.**

---

## Verification recipes

### Schema check (manual)

Pipe through `jq` to verify well-formedness:

```bash
cat $MCP_AUDIT_LOG_PATH | jq -c 'select(.event)' | head
```

Should produce one JSON object per line, each carrying at least
`event`, `ts`, `request_id`, `caller_id`, `tool`.

### Pairing check (every `tool.start` has a `tool.end`)

```bash
jq -r 'select(.event=="tool.start") | .request_id' $MCP_AUDIT_LOG_PATH | sort > /tmp/starts
jq -r 'select(.event=="tool.end") | .request_id'   $MCP_AUDIT_LOG_PATH | sort > /tmp/ends
diff /tmp/starts /tmp/ends
```

Empty diff = perfect pairing. Any unmatched `start` means the server
crashed or was killed mid-call — escalate per
[`INCIDENT_RESPONSE.md`](INCIDENT_RESPONSE.md).

### Caller activity by tool (last hour)

```bash
jq -r 'select(.event=="tool.end" and .ts > (now - 3600)) | "\(.caller_id) \(.tool) \(.outcome)"' \
  $MCP_AUDIT_LOG_PATH \
  | sort | uniq -c | sort -nr | head -20
```

Spikes in `error` or `rate_limited` rates surface immediately.

### Find the SQL behind a slow request

```bash
# 1. Find the request
jq 'select(.event=="tool.end" and .latency_ms > 5000)' $MCP_AUDIT_LOG_PATH

# 2. Find the matching statement_id
jq 'select(.event=="tool.databricks_statement" and .request_id=="<uuid>")' \
   $MCP_AUDIT_LOG_PATH

# 3. Pull the SQL from system.query.history (run as a privileged user)
SELECT statement_text FROM system.query.history WHERE statement_id = '<id>'
```

---

## Schema versioning

The audit-log schema is **v1** as of commit `fc18d73` (the commit
that added `tool.databricks_statement`). Future schema changes will
add fields rather than rename or remove them, so log consumers can
ignore unknown fields and parse old + new logs uniformly.

If a breaking change ever lands, this doc bumps to v2 and includes a
migration recipe. Until then: every record above is stable.

---

## See also

- [`SECURITY.md`](SECURITY.md) — threat model, including the policy
  enforcement model that drives what gets logged
- [`COMPLIANCE.md`](COMPLIANCE.md) — SOC 2 / ISO 27001 mapping; the
  audit log is the evidence for several controls
- [`RUNBOOK.md`](RUNBOOK.md) — operator's guide; "Tracing a request
  through the system" walks the request_id pivot
- [`INCIDENT_RESPONSE.md`](INCIDENT_RESPONSE.md) — incident playbooks
  that begin with audit-log preservation
