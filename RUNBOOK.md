# databricks-ai-steward — Operator Runbook

Concrete recipes for the on-call engineer running this MCP server in
production. Cross-references [`SECURITY.md`](SECURITY.md) for the
threat model and [`STRESS_FINDINGS.md`](STRESS_FINDINGS.md) for the
empirical reliability characteristics.

If you're using this server *locally* from Claude Code / Cursor /
Claude Desktop, ignore this document — the local stdio path is
covered in [`COMPATIBILITY.md`](COMPATIBILITY.md).

---

## One-screen cheatsheet

| Question | Answer |
|---|---|
| Is the server alive? | `curl http://<host>:8765/healthz` → `200 ok` |
| Is the server serving requests? | `curl http://<host>:8765/readyz` → `200 ready` (or `503 draining`) |
| What's it doing right now? | `curl http://<host>:8765/metrics \| grep mcp_in_flight_tools` |
| Where are the logs? | container stderr; audit log at `MCP_AUDIT_LOG_PATH` |
| Where are the metrics? | `/metrics` on the same port (Prometheus exposition format) |
| Where are the traces? | wherever your `OTEL_EXPORTER_OTLP_ENDPOINT` points |
| Stop accepting new requests but finish in-flight | send `SIGTERM` |
| Hard kill | send `SIGKILL` (last resort — drops in-flight) |

---

## Deployment

### Container

```bash
docker run --rm \
  -p 8765:8765 \
  -v /etc/databricks-ai-steward/audit:/var/log/databricks-ai-steward \
  -e DATABRICKS_HOST=https://<workspace>.cloud.databricks.com \
  -e DATABRICKS_TOKEN=<see "Rotating credentials" below> \
  -e MCP_BEARER_TOKEN=<32+ random bytes> \
  -e MCP_BEARER_TOKEN_NAME=team-data \
  -e MCP_AUDIT_LOG_PATH=/var/log/databricks-ai-steward/audit.jsonl \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=https://otel-collector.internal:4318 \
  -e MCP_PROMETHEUS_ENABLED=1 \
  ghcr.io/<your-org>/databricks-ai-steward:<sha>
```

Default container env in the Dockerfile already sets `MCP_TRANSPORT=streamable-http`,
`MCP_HOST=0.0.0.0`, `MCP_PORT=8765`, and `MCP_ALLOW_EXTERNAL=1`.

### Kubernetes

```yaml
apiVersion: apps/v1
kind: Deployment
spec:
  replicas: 2
  template:
    spec:
      containers:
      - name: mcp
        image: ghcr.io/<your-org>/databricks-ai-steward:<sha>
        ports:
        - containerPort: 8765
        envFrom:
        - secretRef: {name: databricks-ai-steward-secrets}
        livenessProbe:
          httpGet: {path: /healthz, port: 8765}
          periodSeconds: 30
          failureThreshold: 3
        readinessProbe:
          httpGet: {path: /readyz, port: 8765}
          periodSeconds: 5
        resources:
          requests: {cpu: 100m, memory: 256Mi}
          limits:   {cpu:   1, memory:   1Gi}
        securityContext:
          runAsNonRoot: true
          runAsUser:    10001
          allowPrivilegeEscalation: false
          readOnlyRootFilesystem: true   # see "Audit log volume" below
        volumeMounts:
        - name: audit
          mountPath: /var/log/databricks-ai-steward
      volumes:
      - name: audit
        persistentVolumeClaim:
          claimName: databricks-ai-steward-audit
```

> **Audit log volume.** With `readOnlyRootFilesystem: true`, the
> audit log path needs a writable mount. Mount a PVC (or an emptyDir
> if you don't need durability across pod restarts; the log shipper
> will have already drained it) at
> `/var/log/databricks-ai-steward/`.

### Multi-replica considerations

The rate limiter is **per-process**. Two replicas means each gets its
own bucket — effective rate limit doubles. Tune `MCP_RATE_LIMIT`
accordingly or move to a shared backend (Redis token bucket) if
strict global ceilings matter.

The audit log is also **per-process**. Aggregate via a sidecar log
shipper (Vector, Filebeat, Fluent Bit) reading the JSONL file or
stderr; records are already in a shape friendly to those tools.

---

## Configuration reference

| Variable | Effect | Default |
|---|---|---|
| `DATABRICKS_HOST` | Workspace URL | required |
| `DATABRICKS_TOKEN` | Default PAT, used when no per-tool token is configured | required |
| `MCP_TOOL_TOKEN_<TOOL>` | Per-tool PAT — the steward picks this client when running `<tool>` | unset |
| `MCP_DATABRICKS_WAREHOUSE_ID` | Default SQL warehouse for `execute_sql_safe` | first running |
| `MCP_TRANSPORT` | `stdio` / `sse` / `streamable-http` | `stdio` |
| `MCP_HOST` / `MCP_PORT` | HTTP bind | `127.0.0.1` / `8765` |
| `MCP_ALLOW_EXTERNAL` | Permit non-loopback bind | unset (refuses) |
| `MCP_BEARER_TOKEN` | Required `Authorization: Bearer …` value on HTTP transports | unset |
| `MCP_BEARER_TOKEN_NAME` | Caller identity attached to audit + Databricks `query_tags` for authenticated requests | `bearer-authenticated` |
| `MCP_RATE_LIMIT` | Per-tool quota overrides, e.g. `execute_sql_safe=10/60,*=200/60` | defaults |
| `MCP_TOOL_TIMEOUT_S` | Outer per-tool timeout via `_guard` | `30` |
| `MCP_SQL_WAIT_TIMEOUT_S` | Databricks Statement-Execution `wait_timeout` | `25` |
| `MCP_SQL_ROW_LIMIT` / `MCP_SQL_HARD_ROW_LIMIT` | Default and hard cap for SQL row limit | `100` / `1000` |
| `MCP_MAX_RESPONSE_BYTES` | Cap on tool response size | `262144` |
| `MCP_SHUTDOWN_GRACE_S` / `MCP_CLEANUP_TIMEOUT_S` | stdio graceful-shutdown deadlines | `5` / `2` |
| `MCP_AUDIT_LOG_PATH` | JSONL audit log path | unset (stderr only) |
| `MCP_AUDIT_DISABLE_STDERR` | Suppress audit-log emission to stderr | unset |
| `MCP_CALLER_ID` | Default caller identity for this process | `unknown` |
| `MCP_LOG_LEVEL` | Root logger level | `WARNING` |
| `MCP_PROMETHEUS_ENABLED` | If `1`, mount `/metrics` and record counters | unset |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP collector — enables tracing when set | unset |
| `OTEL_SERVICE_NAME` | Service name attached to spans | `databricks-ai-steward` |

Full threat-model context is in [`SECURITY.md`](SECURITY.md).

---

## Observability

### Three signals you have

1. **Audit log** (`MCP_AUDIT_LOG_PATH` JSONL) — every tool call's
   `start` and `end` records sharing a `request_id`, with caller
   identity, latency, outcome, error type, response bytes. Argument
   *values* are deliberately not logged (only names + a digest) —
   forensic replay needs Databricks-side `system.access.audit` and
   `system.query.history`, which the server tags with the MCP caller
   identity via `query_tags`.

2. **Metrics** (Prometheus, `/metrics`) —
   - `mcp_tool_calls_total{tool, caller, outcome}` — count of completed
     tool calls. `outcome` values: `success`, `error`, `rate_limited`,
     `timeout`.
   - `mcp_tool_call_duration_seconds{tool, outcome}` — histogram of
     end-to-end latency per tool.
   - `mcp_in_flight_tools` — gauge of currently-executing calls.

3. **Traces** (OpenTelemetry OTLP, `OTEL_EXPORTER_OTLP_ENDPOINT`) —
   one span per tool call, named `mcp.tool.<name>`, with attributes
   `mcp.tool.name`, `mcp.request.id`, `mcp.caller.id`. The
   `request_id` is identical to the one in the audit log so traces
   and audit records correlate one-to-one.

### Suggested alerts

| Alert | PromQL | Why |
|---|---|---|
| Server not ready | `up{job="databricks-ai-steward"} == 0` | basic liveness |
| Sustained errors | `sum(rate(mcp_tool_calls_total{outcome="error"}[5m])) by (tool) > 0.1` | tool-level error rate over 5 min |
| Tool timeout flurry | `sum(rate(mcp_tool_calls_total{outcome="timeout"}[5m])) by (tool) > 0.05` | warehouse / network slowness |
| Rate-limit pressure | `sum(rate(mcp_tool_calls_total{outcome="rate_limited"}[5m])) > 0.1` | a caller is hitting quota — investigate |
| Long p99 latency | `histogram_quantile(0.99, rate(mcp_tool_call_duration_seconds_bucket[5m])) > 30` | warehouse cold-start or SQL regression |
| In-flight saturation | `mcp_in_flight_tools > 50` | concurrent SQL is consuming the connection pool — see SECURITY.md known-limitation #6 |

---

## Troubleshooting

### Symptom: `401 unauthorized` on `/mcp`
The HTTP request lacks `Authorization: Bearer $MCP_BEARER_TOKEN`.
Probes (`/healthz`, `/readyz`, `/metrics`) deliberately bypass auth;
everything else requires it. Compare client config; rotate the token
if you suspect compromise.

### Symptom: `Refusing to bind '0.0.0.0'…` at startup
Set `MCP_ALLOW_EXTERNAL=1`. The server refuses non-loopback binds by
default to prevent accidental exposure during dev.

### Symptom: Tool call returns `{"error": {"type": "RateLimitExceeded"}}`
A caller (identified by `MCP_BEARER_TOKEN_NAME` or `MCP_CALLER_ID`) is
above the per-(tool, caller) quota. Defaults: `execute_sql_safe`
5/min, audit/billing/query-history 10/min, metadata 50/min. Override
via `MCP_RATE_LIMIT="execute_sql_safe=10/60,*=100/60"`. Inspect the
audit log for the offending caller's recent records.

### Symptom: Tool call returns `{"error": {"type": "ToolTimeout"}}`
The outer guard timeout fired. Default 30s; SQL tools bump to 60s.
The actual statement is cancelled server-side by Databricks because
`on_wait_timeout=CANCEL`. Common causes: cold-starting a warehouse,
querying a high-volume system table on a small warehouse (see
SECURITY.md known-limitation #5), or genuinely slow SQL.

### Symptom: `recent_audit_events` always returns `StatementFailed: CANCELED` on a 2X-Small warehouse
Documented limitation. Move to a larger warehouse, or call
`execute_sql_safe` directly with tighter predicates (e.g.
`WHERE service_name = 'unityCatalog'`).

### Symptom: server hangs at shutdown
Sync tools wedge the asyncio loop and prevent stdin-EOF detection.
`safe_tool` rejects sync tools at registration by default — if you
have one, it was registered with `allow_sync=True`. Either remove
that flag or accept that shutdown will need SIGKILL after the
configured grace.

### Symptom: clients see "session is wedged" after a timeout
This is the upstream MCP Python SDK bug we filed as
[python-sdk#2507](https://github.com/modelcontextprotocol/python-sdk/issues/2507).
Until the SDK ships a fix, the server-side per-tool timeout
(`MCP_TOOL_TIMEOUT_S`) is the workaround that bounds resource leaks.

### Symptom: stdout corruption / "MCP transport saw parse error"
Some tool or imported library wrote to stdout. The whole project pins
logging to stderr (mcp_server/app.py) and `safe_tool` doesn't allow
sync tools (which historically had `print()` calls). If you see this,
look for `print(...)` in newly-added tool code or in third-party
deps that the SDK might call. `stress/probe_stdout_clean.py`
reproduces and validates the clean state.

### Symptom: high fd / RSS growth under SQL load
Each concurrent `execute_sql_safe` opens its own httpx connection
(~2 fds, ~3 MB RSS — measured in `probe_sql_concurrency`). Bounded
but not free. If you're sustaining > 50 concurrent SQL calls per
process, add a horizontal replica rather than scaling up.

---

## Rotating credentials

### Default Databricks PAT (`DATABRICKS_TOKEN`)

1. Mint a new PAT in the Databricks workspace.
2. Update the secret store (k8s Secret, AWS SM, Vault).
3. Roll the deployment — `kubectl rollout restart`.
4. Confirm `/readyz` is healthy on the new pods before revoking the
   old token.
5. Revoke the old PAT in the workspace.

### Per-tool PATs (`MCP_TOOL_TOKEN_<TOOL>`)

Same process, narrower blast radius. The recommended pattern is one
Databricks service principal per tool, each with the minimum grants
that tool needs. The steward then never holds a credential broad
enough to exfiltrate everything.

### Bearer token (`MCP_BEARER_TOKEN`)

1. Generate a new value: `openssl rand -hex 32`.
2. Update the secret store.
3. Roll the deployment.
4. Update every client at the same time. There is no "old + new
   accepted simultaneously" support — if you need overlap, run two
   deployments behind a path-routing proxy and decommission the old
   one when client traffic drops.

---

## Rolling back

If a release misbehaves, the safe path is image rollback:

```bash
kubectl set image deploy/databricks-ai-steward mcp=ghcr.io/<your-org>/databricks-ai-steward:<previous-sha>
kubectl rollout status deploy/databricks-ai-steward
```

The reliability characteristics are stable across rollbacks — the
Tier 1 / Tier 2 / Tier 3 hardening is additive. A rollback re-introduces
gaps documented in earlier `SECURITY.md` revisions but does not break
backward compatibility on the wire (MCP protocol versions are
negotiated by FastMCP).

If the audit log or rate limiter is the suspected culprit, you can
also disable just that piece without rolling back:
- Audit: clear `MCP_AUDIT_LOG_PATH` (records still go to stderr).
- Rate limit: `MCP_RATE_LIMIT="*=999999/60"` effectively disables.
- Telemetry: unset `OTEL_EXPORTER_OTLP_ENDPOINT` and
  `MCP_PROMETHEUS_ENABLED`.

---

## Scaling

| Bottleneck | Symptom | Action |
|---|---|---|
| Per-process httpx pool growth | `mcp_in_flight_tools` ≥ 50, fd count climbing | Add replicas |
| Rate-limit cap globally | `outcome="rate_limited"` rate spikes | Raise `MCP_RATE_LIMIT` ceilings or distribute callers |
| Workspace API 429s | SDK auto-retries, persistent 429 surfaces as `TooManyRequests` errors | Lower the steward's effective load (per-tool rate limits) or contact your Databricks admin to raise the workspace quota |
| Warehouse capacity | `outcome="timeout"` rate climbs, `mcp_tool_call_duration_seconds` p99 climbs | Resize the SQL warehouse or move to a larger size |
| Audit log disk | the configured path fills | Rotate the file (logrotate / k8s sidecar) and ensure your log shipper is keeping up |

---

## Where things live

| What | Where |
|---|---|
| Source of truth | https://github.com/sherman94062/databricks-ai-steward |
| CI status | GitHub Actions on `main` |
| Threat model | [`SECURITY.md`](SECURITY.md) |
| Stress findings | [`STRESS_FINDINGS.md`](STRESS_FINDINGS.md) |
| Compatibility matrix | [`COMPATIBILITY.md`](COMPATIBILITY.md) |
| Failure-mode catalog | [`FAILURE_MODES.md`](FAILURE_MODES.md) |
| Tool authoring | [`WALKTHROUGH.md`](WALKTHROUGH.md) |
| Request-flow narrative | [`FLOW.md`](FLOW.md) |
| Upstream issues we filed | [python-sdk#2507](https://github.com/modelcontextprotocol/python-sdk/issues/2507) |
