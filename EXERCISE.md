# databricks-ai-steward — Feature Exercise Guide

A 9-phase, hands-on guide that walks through every public surface of
this project. Run phases top-to-bottom; each takes 5–15 minutes. By
the end you will have hit every tool, transport, guard, and
deployment shape, and seen the audit-trail-as-evidence story
end-to-end.

> **Print-friendly version.** A standalone HTML rendering of this
> guide with a print-tuned stylesheet (page breaks, page numbers,
> code blocks that don't get sliced across pages) lives at
> [`docs/EXERCISE.html`](docs/EXERCISE.html). Open in any browser →
> Cmd-P / Ctrl-P → Save as PDF or print.

Reference docs to keep open: [`RUNBOOK.md`](RUNBOOK.md),
[`SECURITY.md`](SECURITY.md),
[`AUDIT_LOG_SCHEMA.md`](AUDIT_LOG_SCHEMA.md),
[`STRESS_FINDINGS.md`](STRESS_FINDINGS.md).

---

## Phase 0 — One-time setup

```bash
cd <repo-root>
source .venv/bin/activate
pip install -e '.[dev,http]'

# Confirm .env has DATABRICKS_HOST + DATABRICKS_TOKEN
cat .env | grep -E '^DATABRICKS_' | sed 's/=.*/=<set>/'
# Should print:
#   DATABRICKS_HOST=<set>
#   DATABRICKS_TOKEN=<set>
```

---

## Phase 1 — Verify the test surface

**Goal:** confirm the 102-test suite passes locally exactly as CI
sees it.

```bash
pytest tests/ -v
# Expect: 102 passed
```

Then poke at individual subsystems so you know where each lives:

```bash
pytest tests/test_audit_chain.py -v          # hash chain + verifier
pytest tests/test_audit_and_rate_limit.py -v # rate limiter + bearer/X-End-User
pytest tests/test_sql_safety.py -v           # sqlglot governance gate
pytest tests/test_sql_tools.py -v            # execute_sql_safe end-to-end (mocked workspace)
pytest tests/test_guards.py -v               # safe_tool guards (timeout, size cap)
```

Watch the test names — they read as a spec of what each subsystem
promises.

---

## Phase 2 — Run the server (stdio) + Claude Code wiring

```bash
# Smoke-start it. It will block waiting for an MCP client on stdin.
python -m mcp_server.server
# Press Ctrl-C to kill.
```

Now register it with Claude Code so you can call the tools from a
real client:

```bash
claude mcp add databricks-steward -- python -m mcp_server.server
claude mcp list
```

In a Claude Code session, ask it to call the tools — for example:

> "Use the databricks-steward MCP server to call the `health` tool
> and show the result."

Confirm it returns `{status, ready, version, uptime_s,
in_flight_tasks}` and `ready: true`.

> "List the catalogs in the workspace."

Confirm `list_catalogs` returns real Unity Catalog data.

---

## Phase 3 — Exercise the live Databricks tools

These all go through `execute_sql_safe` and the governance gate.
Drive them from Claude Code (or directly via the MCP Inspector —
`npx @modelcontextprotocol/inspector python -m mcp_server.server`):

```text
list_catalogs                    # UC catalog enumeration
health                           # liveness + drain state
list_system_tables               # readable tables in `system` catalog
execute_sql_safe(query="SHOW TABLES IN system.information_schema")
execute_sql_safe(query="SELECT current_user()")
recent_query_history(limit=10)
billing_summary(since_days=7)
recent_audit_events(limit=5)     # may time out on a 2X-Small warehouse — see RUNBOOK
```

Now poke at the **governance gate** — these should all be rejected
before reaching Databricks:

```text
execute_sql_safe(query="DROP TABLE foo")          # → SqlNotAllowed
execute_sql_safe(query="DELETE FROM foo")         # → SqlNotAllowed
execute_sql_safe(query="UPDATE foo SET x=1")      # → SqlNotAllowed
execute_sql_safe(query="GRANT SELECT ON foo TO bar")  # → SqlNotAllowed
execute_sql_safe(query="SELECT * FROM foo; DROP TABLE bar")  # multi-stmt → SqlNotAllowed
```

Confirm each comes back as `{"error": {"type": "SqlNotAllowed",
...}}` rather than executing.

---

## Phase 4 — The audit log + tamper-evidence

```bash
# Point the server at a writable audit file.
export MCP_AUDIT_LOG_PATH=/tmp/steward-audit.jsonl
rm -f $MCP_AUDIT_LOG_PATH

# Restart the stdio server so it picks up the env. Then run a few
# tools from your client (Phase 3). When done, inspect the JSONL:
cat $MCP_AUDIT_LOG_PATH | jq -c '{event, tool, request_id, seq, outcome}' | head -10
```

Pivot a single request through the log:

```bash
# Pick any request_id from the file, then:
RID=<paste-request-id>
jq "select(.request_id == \"$RID\")" $MCP_AUDIT_LOG_PATH
```

You should see `tool.start` → (optional `tool.databricks_statement`)
→ `tool.end`, all sharing the same `request_id`. Take the
`statement_id` from `tool.databricks_statement` and look it up in
Databricks' `system.query.history` — you've now done the
`request_id → statement_id` pivot the runbook describes.

**Verify the chain:**

```bash
python -m mcp_server.audit_verify $MCP_AUDIT_LOG_PATH
# Expect: chain intact: N record(s) verified
```

**Now tamper with it and confirm detection:**

```bash
# Edit the first record's `tool` field by hand (any text editor) — keep
# its seq/prev_hash/hash intact. Save, then:
python -m mcp_server.audit_verify $MCP_AUDIT_LOG_PATH
# Expect exit code 1, "hash mismatch"
echo $?
```

Restore from a backup or delete and re-generate. You've now seen
tamper-evidence from both sides.

---

## Phase 5 — HTTP transport + bearer auth + per-end-user identity

```bash
# Generate a bearer token + start over HTTP, loopback-only.
export MCP_BEARER_TOKEN=$(openssl rand -hex 32)
export MCP_BEARER_TOKEN_NAME=team-data
python -m mcp_server.server --transport streamable-http --port 8765 &
SERVER_PID=$!
sleep 2
```

Test the probe endpoints (no auth needed):

```bash
curl -s http://127.0.0.1:8765/healthz   # → ok
curl -s http://127.0.0.1:8765/readyz    # → ready
```

Confirm the API surface refuses without the token:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/mcp
# → 401
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $MCP_BEARER_TOKEN" \
  http://127.0.0.1:8765/mcp
# → 200 (or 4xx on a missing session header — auth is what we're testing)
```

Confirm the **external-bind refusal**:

```bash
python -m mcp_server.server --transport streamable-http --host 0.0.0.0 --port 8765
# Should refuse: "Refusing to bind '0.0.0.0': external bind requires --allow-external"
```

Stop the server: `kill $SERVER_PID`.

**Per-end-user identity (X-End-User):**

```bash
export MCP_TRUST_END_USER_HEADER=1
export MCP_END_USER_HEADER=X-Forwarded-User
python -m mcp_server.server --transport streamable-http --port 8765 &
SERVER_PID=$!
sleep 2

# Drive a tool call with the header set — caller_id should pick it up.
# Easiest path: write a small request via the MCP Inspector or use the
# probe at stress/probe_http_auth.py (see Phase 6).
kill $SERVER_PID
unset MCP_TRUST_END_USER_HEADER MCP_END_USER_HEADER MCP_BEARER_TOKEN MCP_BEARER_TOKEN_NAME
```

Then check the audit log: `caller_id` should equal whatever the
header carried, not `team-data`.

---

## Phase 6 — Reliability layer (stress probes)

These are the most fun part — each probe surfaces a specific real
bug or verifies a specific guard. Run them one at a time and read
the printout:

```bash
# Throughput baseline — single session, configurable concurrency.
python -m stress.load --concurrent 50 --total 1000

# Async cancellation: probe_a1_leak shows the bug,
# probe_a1_fix_verify confirms the fix.
python -m stress.probe_a1_leak
python -m stress.probe_a1_fix_verify

# Sync-tool blast radius — why @safe_tool rejects sync defs by default.
python -m stress.probe_d_blast_radius

# Graceful drain on SIGTERM/SIGINT.
python -m stress.probe_restart

# SQL governance gate (real Databricks).
python -m stress.probe_sql_governance

# SQL cancellation propagation.
python -m stress.probe_sql_cancellation

# Soak — long-running real Databricks load, smoke-tests fd / RSS growth.
python -m stress.probe_databricks_soak

# HTTP transport probes.
python -m stress.probe_http_auth
python -m stress.probe_http_lifecycle
python -m stress.probe_http_transport

# MCP-specific threat probes (prompt injection, session confusion).
python -m stress.probe_adversarial

# Token-scrubbing probe — confirms tokens never appear in audit/error.
python -m stress.probe_token_audit

# Stdout-cleanliness — anything written to stdout other than JSON-RPC
# corrupts the stdio session; this probe asserts nothing leaks.
python -m stress.probe_stdout_clean
```

Each prints PASS/FAIL with a short narrative.
[`STRESS_FINDINGS.md`](STRESS_FINDINGS.md) documents the bug each
probe was originally written to reproduce.

---

## Phase 7 — Rate limiting + per-tool credentials

### Rate limiting

```bash
export MCP_RATE_LIMIT='execute_sql_safe=2/60,*=50/60'
export MCP_AUDIT_LOG_PATH=/tmp/rl-test.jsonl
rm -f $MCP_AUDIT_LOG_PATH

# Start server (stdio or HTTP), call execute_sql_safe three times.
# The third call returns:
#   {"error": {"type": "RateLimitExceeded", ...}}
# Check the audit log:
jq -c 'select(.event == "tool.rate_limit_exceeded")' $MCP_AUDIT_LOG_PATH
```

You should see one record per refusal carrying `limit: 2`,
`window_s: 60`.

### Per-tool credentials

```bash
# Default: every tool uses DATABRICKS_TOKEN.
# Override one tool with its own PAT:
export MCP_TOOL_TOKEN_LIST_CATALOGS=dapi<another-token>

# Now `list_catalogs` runs as the second token's identity. The other
# tools still use DATABRICKS_TOKEN. Verify by checking the
# `mcp_caller=<id>` query_tag in `system.query.history` for an
# execute_sql_safe call vs a list_catalogs call (they go through
# different SDK clients).
```

---

## Phase 8 — Telemetry (Prometheus + OpenTelemetry)

### Prometheus

```bash
export MCP_PROMETHEUS_ENABLED=1
python -m mcp_server.server --transport streamable-http --port 8765 &
SERVER_PID=$!
sleep 2

curl -s http://127.0.0.1:8765/metrics | head -30
# Look for:
#   mcp_tool_calls_total{tool="...",outcome="..."}
#   mcp_tool_latency_ms_bucket{tool="..."}
#   mcp_in_flight_tools

# Drive a few tool calls, then re-fetch /metrics and watch counters move.
kill $SERVER_PID
```

### OpenTelemetry traces (optional, needs a collector)

```bash
# Stand up an OTLP collector locally (Jaeger all-in-one is the easiest).
docker run --rm -d --name jaeger -p 16686:16686 -p 4317:4317 \
  jaegertracing/all-in-one:latest

export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317
export OTEL_SERVICE_NAME=databricks-ai-steward
python -m mcp_server.server --transport streamable-http --port 8765 &
sleep 2

# Drive tool calls, then open Jaeger:
open http://127.0.0.1:16686
# Search for service `databricks-ai-steward`. Each tool call shows up
# as a span with the same `request_id` you see in the audit log.

kill %1
docker stop jaeger
```

---

## Phase 8.5 — MCP Prompts (CFO-readable billing)

The server exposes one Prompt today: `billing_report`. Prompts are
user-initiated templates (vs. Tools, which the LLM calls) — pick one
in the client UI, fill in the args, and the rendered template
becomes the prompt the LLM sees.

In Claude Code, type `/` and you should see `billing_report` in the
prompt picker. Pick it, enter `weeks_back: 1`, and the LLM
will call `billing_summary` (twice, for current + prior period),
translate DBU/DSU/SKU output to dollars + plain English, and emit a
report under 200 words formatted for leadership.

In MCP Inspector, switch to the **Prompts** tab. `billing_report`
should appear with a `weeks_back` argument. Click "Get Prompt" — the
template returns instructions to the LLM (not the report itself; the
LLM still has to do the work via tool calls).

The prompt only renders well end-to-end when `MCP_DBU_RATE_CARD` is
configured (see Phase 8). Without it, the LLM will note that prices
weren't configured rather than guessing.

---

## Phase 9 — Containerized + helm

### Docker Compose (single-host)

```bash
docker compose -f deploy/docker-compose.yml up --build
# In another terminal:
curl -s http://127.0.0.1:8765/healthz   # → ok
docker compose -f deploy/docker-compose.yml down
```

### Helm chart (template only — no cluster needed)

```bash
helm lint deploy/helm/databricks-ai-steward
helm template steward deploy/helm/databricks-ai-steward | less

# With overrides — ingress on, oauth2-proxy + X-Forwarded-User wiring,
# Prometheus, OTel, log-shipper sidecar:
cat > /tmp/values.yaml <<'EOF'
secrets:
  databricks: { secretName: dbx-creds }
  bearer:     { secretName: steward-bearer }
ingress:
  enabled: true
  hosts:
    - host: steward.example.com
      paths: [ { path: /, pathType: Prefix } ]
config:
  trustEndUserHeader: "1"
  endUserHeader: X-Forwarded-User
  bearerTokenName: team-data
  prometheusEnabled: "1"
networkPolicy:
  databricks: { host: workspace.cloud.databricks.com }
serviceMonitor: { enabled: true }
EOF
helm template steward deploy/helm/databricks-ai-steward -f /tmp/values.yaml | less
```

### Apply against a real cluster (optional)

```bash
kubectl create secret generic dbx-creds \
  --from-literal=host="$DATABRICKS_HOST" \
  --from-literal=token="$DATABRICKS_TOKEN"
kubectl create secret generic steward-bearer \
  --from-literal=token=$(openssl rand -hex 32)

helm install steward deploy/helm/databricks-ai-steward \
  --set secrets.databricks.secretName=dbx-creds \
  --set secrets.bearer.secretName=steward-bearer \
  --set image.repository=ghcr.io/sherman94062/databricks-ai-steward \
  --set image.tag=latest

kubectl get pods -l app.kubernetes.io/name=databricks-ai-steward -w
kubectl port-forward svc/steward-databricks-ai-steward 8765:8765
# Hit http://127.0.0.1:8765/healthz from another terminal.
```

---

## What you'll have exercised by the end

| Phase | Subsystem | Key artifact you'll have seen |
|---|---|---|
| 1 | Test surface | 102 passing tests across 9 modules |
| 2 | Stdio + Claude Code | Server registered, tools invokable from a real client |
| 3 | Live tools + governance | Real catalogs / SQL / system tables; gate rejects DDL |
| 4 | Audit log + chain | `request_id` pivot + `audit_verify` detecting tampering |
| 5 | HTTP + auth | Probes bypass auth; `/mcp` requires it; X-End-User propagates |
| 6 | Reliability probes | 14 probes, each surfacing a specific real bug or guard |
| 7 | Rate limit + per-tool creds | Refusal record in audit; `query_tags` differ per tool |
| 8 | Telemetry | `/metrics` counters move; Jaeger spans share `request_id` |
| 9 | Deployment | Compose works; helm renders + applies cleanly |

When you're done, you'll have hit every public surface of the
project and seen the audit-trail-as-evidence story end-to-end. If a
phase reveals something unexpected, the four reference docs cover
almost everything: [`RUNBOOK.md`](RUNBOOK.md) for ops,
[`SECURITY.md`](SECURITY.md) for the threat model,
[`AUDIT_LOG_SCHEMA.md`](AUDIT_LOG_SCHEMA.md) for the audit-log
contract, [`STRESS_FINDINGS.md`](STRESS_FINDINGS.md) for what each
probe was originally written to catch.
