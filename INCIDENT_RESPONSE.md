# databricks-ai-steward — Incident Response

This is the playbook for **compromise scenarios**, distinct from
[`RUNBOOK.md`](RUNBOOK.md) which covers operational issues ("server
is slow", "tool is timing out"). When the question shifts from
*"why isn't this working"* to *"is this thing owned"*, you're here.

Each scenario follows a five-phase structure (NIST 800-61): **detect →
contain → eradicate → recover → lessons learned**. The first action
in every scenario is the same — preserve evidence — so it's listed
once at the top.

---

## Always do first

1. **Preserve audit logs.** Snapshot `MCP_AUDIT_LOG_PATH` (or the
   container's stderr stream collected by your log shipper) immediately.
   `cp $MCP_AUDIT_LOG_PATH /secure/incident/<timestamp>-audit.jsonl`,
   chmod 400. Records here are the canonical answer to "who called
   what tool with what arguments" — argument *values* aren't logged
   but the digest + names are enough to bound blast radius.
2. **Preserve Databricks query history.** From a privileged session:
   ```sql
   SELECT * FROM system.query.history
   WHERE start_time > current_timestamp() - INTERVAL 24 HOURS
     AND mcp_caller IS NOT NULL  -- our query_tags
   ```
   Save the result. This is the *what actually ran* against the
   workspace — whereas the audit log is *what the steward was asked
   to do*. Diffing the two surfaces tampering.
3. **Preserve the container image (if HTTP deployment).**
   `docker image save databricks-ai-steward:<sha> > /secure/incident/<timestamp>-image.tar`.
4. **Note the `MCP_BEARER_TOKEN_NAME`** values seen in recent audit
   records. That's the set of clients with valid bearer auth at
   incident time.

---

## Scenario 1 — `DATABRICKS_TOKEN` (or `MCP_TOOL_TOKEN_*`) leaked

**Detect**:
- The token appeared in a public commit, container layer, screen
  recording, log line, or chat transcript.
- Or: anomalous Databricks workspace activity — queries from IPs
  outside expected ranges, queries against catalogs the steward
  shouldn't be hitting.

**Contain (within 5 minutes of detection)**:
1. Revoke the token in the Databricks workspace UI: **User Settings →
   Access tokens → Revoke**. For service principal tokens: **Account
   Console → Service principals → token list → Revoke**.
2. If you don't know which token, revoke *all* steward-owned tokens.
   Cost is minutes of downtime; benefit is certainty.
3. Stop the steward: `kubectl scale deploy/databricks-ai-steward --replicas=0`
   (or `docker stop`). Without a valid token the steward returns
   structured errors; revoke first, stop second so you don't leave a
   window where the leaked token still works against an offline
   service that "looks" stopped from the operator side.

**Eradicate**:
1. Mint a replacement token (or set up a new service principal if the
   old SP itself is suspect).
2. Update the secret store (Vault / AWS SM / k8s Secret).
3. `kubectl rollout restart` once the new token is in place.

**Recover**:
1. Verify `/healthz` and `/readyz` on the new pods.
2. Check `system.query.history` for any queries between leak time and
   revocation time. Any entry with the leaked token's identity that
   the steward didn't trigger (compare with the steward's audit log)
   is a real intrusion.
3. If real intrusion: escalate per Zepz IR policy. Treat data the
   token could `SELECT` from as potentially exfiltrated.

**Lessons learned**:
- How was the token exposed? Add a control to prevent that class
  (e.g. tighten log redaction, add `gitleaks` to pre-commit, harden
  CI to mask secrets).
- Was the blast radius too big? If the leaked token had grants beyond
  what the tool needed, that's the trigger to provision a per-tool SP
  via `MCP_TOOL_TOKEN_<TOOL>` (see [`SECURITY.md`](SECURITY.md)
  known-limitation #1).

---

## Scenario 2 — `MCP_BEARER_TOKEN` leaked

**Detect**:
- The bearer token appeared in client-side logs, browser dev tools,
  proxy logs, etc.
- Or: 401 rate suddenly drops to zero from a client you didn't expect
  to be configured (caller_id you don't recognise in audit log).

**Contain (within 5 minutes)**:
1. Generate a new token: `openssl rand -hex 32`.
2. Update secret store + `kubectl rollout restart`. There is no
   "old + new accepted simultaneously" path — clients with the old
   token will get 401 and need to re-fetch.
3. Inform every legitimate client owner so they update their
   credential. (Maintain this list! See **Lessons learned** below.)

**Eradicate**:
- Same as Scenario 1, but the audit log analysis is more nuanced
  because the bearer token authenticates the *caller*, not the
  Databricks identity. Look for `caller_id` values that match the
  legitimate `MCP_BEARER_TOKEN_NAME` but came from unexpected source
  IPs (if your reverse proxy logs source IP, cross-reference).

**Recover, lessons learned**: same shape as Scenario 1.

---

## Scenario 3 — anomalous audit log pattern (probable prompt-injected agent)

**Detect**:
- `mcp_tool_calls_total{outcome="rate_limited"}` rate spikes.
- Audit log shows the same caller_id running an unexpected sequence
  of tool calls (e.g. `list_catalogs` → `list_schemas` →
  `list_tables` → `execute_sql_safe SELECT * FROM payments.…`).
- Databricks query history shows `mcp_caller=...` queries against
  catalogs the steward's normal traffic doesn't touch.

**Contain**:
1. **Tighten the rate limit immediately**: `kubectl set env
   deploy/databricks-ai-steward MCP_RATE_LIMIT="execute_sql_safe=1/300,*=10/60"`
   and `rollout restart`. This caps the leak rate from the suspect
   caller.
2. If the caller is a single bearer-token-name and you can ID it,
   revoke that specific bearer token (Scenario 2 procedure). Other
   callers continue to work.
3. If you can't isolate the caller, treat the entire bearer token
   as compromised and follow Scenario 2.

**Eradicate**:
- Audit the caller's full tool-call history from before the alert
  fired — `grep '"caller_id":"<id>"' $MCP_AUDIT_LOG_PATH | jq .`.
- Identify what data was likely exfiltrated. The audit log shows
  request_ids; cross-reference with `system.query.history` via the
  request_id↔statement_id correlation event to see the actual SQL
  text.
- Engage the upstream agent operator. Was the agent compromised?
  Prompt-injected? Misconfigured?

**Recover**:
- Confirm the rate-limit / token revocation is in effect.
- If real exfiltration occurred, follow Zepz data-incident protocol
  (legal, comms, customer notification per GDPR / regional rules).

**Lessons learned**:
- Were the per-tool rate-limit defaults in `MCP_RATE_LIMIT` too
  generous? Tighten.
- Could a per-tool sub-credential
  (`MCP_TOOL_TOKEN_EXECUTE_SQL_SAFE` with narrow UC grants) have
  bounded the impact? If yes, provision it before re-enabling the
  caller.

---

## Scenario 4 — container escape suspected

**Detect**:
- Trivy scan flagged a kernel-CVE-class issue post-deployment.
- Or: anomalous outbound network traffic from the pod (egress to
  hosts other than the configured `DATABRICKS_HOST` and the OTel
  collector).
- Or: pod's filesystem shows unexpected modifications (host-side
  filesystem audit).

**Contain**:
1. **`kubectl delete pod`** the suspect replica. With
   `replicas=2`+ the deployment self-heals. Do NOT exec into the
   pod first — that risks contaminating evidence.
2. If multiple pods are suspect, drain the node:
   `kubectl drain <node> --delete-emptydir-data --ignore-daemonsets`.
3. Block the node's egress at the network layer if your CNI supports
   it.

**Eradicate**:
1. Image scan the running tag — `trivy image <image>:<sha>`. If
   HIGH/CRITICAL appears, that's the likely vector.
2. Rebuild from a known-good base image (`python:3.12-slim` updated
   to the latest patch). Bump the lockfile to current.
3. Roll forward to the new image. The threat model assumes container
   escapes occur at the OS layer, not the application layer — our
   non-root user, read-only-root-fs, and dropped capabilities mean
   most CVEs don't grant the attacker much even if they land.

**Recover**:
1. Re-scan with trivy: must come back clean (`HIGH=0, CRITICAL=0`).
2. Watch the egress logs for 24h to confirm no residual outbound
   traffic to attacker-controlled hosts.
3. If the credentials inside the pod were exposed (the PAT, the
   bearer token, anything in `.env` mounted into the container),
   follow Scenarios 1 + 2 in parallel.

**Lessons learned**:
- Was the kernel CVE on the running base when we deployed? Tighten
  the gap between Trivy scan and rollout.
- Are our k8s securityContext settings (`runAsNonRoot`,
  `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`,
  `capabilities.drop: [ALL]`) actually applied? Verify in the
  manifest, not just RUNBOOK aspirational text.

---

## Scenario 5 — upstream CVE published in `mcp` or `databricks-sdk`

**Detect**:
- pip-audit fails CI on next push (the most common path).
- Security advisory subscription fires (subscribe to
  `modelcontextprotocol/python-sdk` GitHub releases and the
  `databricks/databricks-sdk-py` advisory feed).

**Contain**:
1. **Severity assessment first.** Read the CVE — does it apply to our
   usage? E.g. an SSE-only CVE doesn't affect a stdio-only deployment.
2. If applicable: bump the dep, regenerate `requirements.lock`
   (`pip-compile pyproject.toml -o requirements.lock`), commit, ship.
3. If a fix is not yet available upstream: open a GitHub issue
   tracking the workaround, decide whether to disable the affected
   transport / feature in the meantime.

**Eradicate, recover, lessons learned**: standard patch cycle. The
audit log captures all tool calls during the window; if exploit
required a specific input pattern, search audit + Databricks query
history for it.

---

## Communication template

Whatever the scenario, escalate via Zepz's standard IR channel with
this skeleton:

> **Severity**: P# (per Zepz severity rubric)
>
> **Service**: databricks-ai-steward (commit `<sha>`, image `<tag>`)
>
> **Detected**: `<UTC timestamp>` via `<source: alert | scan | report>`
>
> **Status**: Contained | Eradicating | Recovered
>
> **Bound on blast radius**:
> - Identities affected: `<list of caller_ids>`
> - Data potentially exposed: `<catalogs / schemas the affected token had grants on>`
> - Time window: `<earliest plausible compromise → containment>`
>
> **Audit log + Databricks query history snapshot**:
> `<storage location, who has access>`
>
> **Next 30 minutes**: `<what's about to happen>`
>
> **Open questions for IR coordinator**: `<list>`

Post updates every 30 minutes during the contain phase, hourly during
eradicate, daily during recover.

---

## What this document is NOT

- It's not a substitute for the Zepz incident-response policy. Where
  Zepz IR procedures conflict with this doc, follow Zepz.
- It's not a guarantee of recovery. The four scenarios above are the
  ones we expect; novel scenarios should escalate to Zepz IR
  immediately rather than improvising from this template.
- It's not legal/regulatory advice. Cross-border money-transfer
  data + GDPR + state breach-notification statutes are why Zepz has
  legal counsel. Loop them in early when customer data is plausibly
  in scope.

---

## See also

- [`RUNBOOK.md`](RUNBOOK.md) — operational issues, not compromises
- [`SECURITY.md`](SECURITY.md) — threat model that informs which
  scenarios are in vs out of scope here
- [`COMPLIANCE.md`](COMPLIANCE.md) — SOC2 / ISO 27001 control mapping
