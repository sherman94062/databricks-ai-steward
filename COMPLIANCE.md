# databricks-ai-steward — Compliance Control Mapping

This document maps the steward's defenses to the SOC 2 Trust Services
Criteria and ISO 27001 Annex A controls a fintech CISO will check
during a pre-production review. Each row links a control to a
*concrete* defense in the codebase plus the test or audit artifact
that proves it works.

This is not a formal attestation. SOC 2 attestation requires an
independent auditor and a defined audit period; ISO 27001
certification requires a registered body. What this *is* is the
groundwork that makes those engagements cheap when they happen — the
controls are already in place, the evidence is already produced
automatically, and the gaps are documented honestly.

---

## SOC 2 Trust Services Criteria

### CC6 — Logical and Physical Access Controls

| Control | Defense | Evidence |
|---|---|---|
| **CC6.1** Implements logical access controls to protect against threats from outside the system boundaries | Bearer-token authentication on HTTP transports (`MCP_BEARER_TOKEN`); refuses non-loopback bind without `MCP_ALLOW_EXTERNAL=1`; loopback-only default | `tests/test_audit_and_rate_limit.py::test_k8s_probes_bypass_bearer_auth_and_track_drain_state`; `mcp_server/server.py:_build_starlette_app` |
| **CC6.2** Authorizes new internal and external users prior to issuing access | Bearer tokens minted out-of-band (operator process); `MCP_BEARER_TOKEN_NAME` records the principal name in audit | `RUNBOOK.md` "Rotating credentials → Bearer token" |
| **CC6.3** Authorizes users; least privilege | Per-tool sub-credentials via `MCP_TOOL_TOKEN_<TOOL>` route each tool to a separate Databricks service principal with narrow Unity Catalog grants | `mcp_server/databricks/client.py::get_workspace`; `tests/test_tool_credentials.py` |
| **CC6.6** Implements logical access controls over the system boundary against threats from outside the entity | Network: refuses `0.0.0.0` bind without explicit override. Identity: bearer token compared timing-safe via `secrets.compare_digest` | `mcp_server/server.py` `_build_starlette_app` |
| **CC6.7** Restricts the transmission, movement, and removal of information | Tool response size cap (`MCP_MAX_RESPONSE_BYTES`, default 256 KB) bounds bulk extraction per call; row-cap on SQL (`MCP_SQL_HARD_ROW_LIMIT`, default 1 000) bounds per-query rows | `mcp_server/app.py::_cap_response`; `mcp_server/tools/sql_tools.py`; `tests/test_sql_tools.py::test_row_limit_capped_at_hard_ceiling` |
| **CC6.8** Implements controls to prevent or detect and act upon the introduction of unauthorized or malicious software | bandit (SAST), ruff (lint), mypy (types), pip-audit (dep CVE), Trivy (container CVE + Dockerfile misconfig) — all gated in CI on every PR | `.github/workflows/ci.yml` |

### CC7 — System Operations

| Control | Defense | Evidence |
|---|---|---|
| **CC7.1** Detects and identifies the introduction of changes that could have compromised the system | All-PRs-required CI; merges blocked on test/lint/type/CVE failures | `.github/workflows/ci.yml`; GitHub branch protection settings (operator-applied — see "Recommended GitHub repo settings" below) |
| **CC7.2** Monitors system components and the operation of those components for anomalies | Three observability signals: JSONL audit log (`MCP_AUDIT_LOG_PATH`), Prometheus `/metrics`, OpenTelemetry traces (`OTEL_EXPORTER_OTLP_ENDPOINT`) | `mcp_server/audit.py`, `mcp_server/telemetry.py`, `RUNBOOK.md` "Suggested alerts" |
| **CC7.3** Evaluates security events to determine whether they could result in failure to meet objectives | `tool.rate_limit_exceeded` audit event + corresponding Prometheus counter + alert template | `mcp_server/audit.py::emit_rate_limit_exceeded`; `RUNBOOK.md` "Suggested alerts" → "Rate-limit pressure" |
| **CC7.4** Responds to identified security incidents | Documented IR playbook for credential leak, anomalous query pattern, container escape, upstream CVE | [`INCIDENT_RESPONSE.md`](INCIDENT_RESPONSE.md) |
| **CC7.5** Identifies and develops security incident recovery activities | Rollback procedure (image rollback + selective feature disable via env vars); audit log preservation procedure | `RUNBOOK.md` "Rolling back"; `INCIDENT_RESPONSE.md` "Always do first" |

### CC8 — Change Management

| Control | Defense | Evidence | Operator action |
|---|---|---|---|
| **CC8.1** Authorizes, designs, develops, configures, documents, tests, approves, and implements changes | Branch protection on `main` (PR review required); CI must pass before merge; signed commits | `.github/workflows/ci.yml`; **Operator: enable branch protection rules in GitHub Settings → Branches → Add rule → main** with "Require pull request reviews before merging", "Require status checks to pass" (`test`, `security`, `container-scan`), "Require signed commits" |

### CC9 — Risk Mitigation

| Control | Defense | Evidence |
|---|---|---|
| **CC9.1** Identifies, selects, and develops risk mitigation activities | Documented threat model (in-scope, out-of-scope, known limitations) including the MCP-specific attack-surface taxonomy | [`SECURITY.md`](SECURITY.md) |
| **CC9.2** Assesses and manages risks associated with vendors and business partners | Pinned dependency lockfile (`requirements.lock`); CycloneDX SBOM published per build; CI gates on dep CVEs and container CVEs | `requirements.lock`; CI artifact `sbom-<sha>.json`; `.github/workflows/ci.yml` jobs `security` and `container-scan` |

---

## Supply-chain posture

A specific area CISOs scrutinise for MCP servers, given the
"tool ecosystem expands attack surface" framing in recent literature.
Our posture, in one paragraph:

**No dynamic tool loading.** Every tool the steward exposes is
statically registered at module-import time via `@safe_tool()`
decorations in `mcp_server/tools/*.py`. There is no plugin discovery,
no `eval`, no dynamic import based on user input, no remote tool
catalogue. The complete tool set is determinable by `grep -r
"@safe_tool" mcp_server/tools/` against any commit. **One external
dependency surface that touches data**: `databricks-sdk`. **One
external dependency surface that touches the network**: `httpx` (used
by the SDK). Both are pinned by exact version in `requirements.lock`,
both are scanned for CVEs by `pip-audit` on every PR, and both are
included in the SBOM (CycloneDX) generated per build. The container
adds OS-level packages from `python:3.12-slim` (Debian-stable), scanned
by Trivy on every PR. Every dependency change goes through PR review +
the CI gates above before reaching `main`.

---

## ISO 27001 Annex A control highlights

Mapping of the most relevant Annex A controls. A full ISO 27001
engagement would also cover A.5–A.7 (governance + HR + asset
management) which are organizational controls outside this service's
scope.

| Annex A | Control | Defense |
|---|---|---|
| **A.5.7** | Threat intelligence | Subscribe to `modelcontextprotocol/python-sdk` releases + `databricks/databricks-sdk-py` advisories; pip-audit catches public CVEs in CI |
| **A.8.2** | Privileged access rights | Per-tool credentials abstraction enables principle of least privilege per tool |
| **A.8.3** | Information access restriction | Bearer auth + row-cap + size-cap + governance gate (`sql_safety.classify`) |
| **A.8.4** | Access to source code | GitHub permissions; protected `main` branch (operator-configured) |
| **A.8.7** | Protection against malware | Container CVE scan (Trivy); base image is `python:3.12-slim` (Debian-stable); non-root + read-only-root FS |
| **A.8.8** | Management of technical vulnerabilities | pip-audit + Trivy gate every PR; lockfile means a vuln triggers a single bump + rebuild |
| **A.8.10** | Information deletion | Audit log retention (operator-configured at log-shipper layer); Databricks data retention is workspace-level, outside steward scope |
| **A.8.12** | Data leakage prevention | Argument *values* deliberately excluded from audit log (digest only); `_scrub` redacts host + token from error messages; row + size caps on tool responses |
| **A.8.15** | Logging | Structured JSONL audit log (`tool.start` + `tool.end` + `tool.rate_limit_exceeded` + `tool.databricks_statement`); shareable correlation IDs |
| **A.8.16** | Monitoring activities | Prometheus metrics (counters, histograms, gauges) + OpenTelemetry traces; sample alerts in RUNBOOK |
| **A.8.17** | Clock synchronization | Container relies on host NTP (deployment concern, documented in RUNBOOK k8s manifest) |
| **A.8.23** | Web filtering | Egress is exclusively to `DATABRICKS_HOST` + optional OTel collector; deployment-time network policy (operator) |
| **A.8.24** | Use of cryptography | TLS via reverse proxy (operator); bearer compare via `secrets.compare_digest` (timing-safe) |
| **A.8.25** | Secure development life cycle | CI runs lint + type + SAST + dep-CVE + container-CVE on every PR |
| **A.8.28** | Secure coding | sqlglot parse-tree governance (not regex); `try/finally` resource cleanup; ruff B-rules enforced |
| **A.8.29** | Security testing in development | 89-test unit suite + 13+ live stress probes |
| **A.8.32** | Change management | Branch protection (operator) + CI gates + lockfile |
| **A.8.34** | Protection of information systems during audit testing | All security tools run in CI without secrets — they scan deps + image, not workspace data |
| **A.5.30** | ICT readiness for business continuity | Stateless service + lifecycle-handler graceful drain + rolling-restart compatible (k8s readinessProbe flips during shutdown) |

---

## Recommended GitHub repo settings (operator-applied)

CI runs the gates; branch protection enforces that the gates apply
to every change. These need to be enabled in **Settings → Branches
→ Branch protection rules → main**:

- [ ] Require a pull request before merging
- [ ] Require approvals (1 minimum, more for fintech)
- [ ] Dismiss stale pull request approvals when new commits are pushed
- [ ] Require status checks to pass before merging:
  - `test (3.12, ubuntu-latest)`
  - `test (3.14, ubuntu-latest)`
  - `security`
  - `container-scan`
- [ ] Require branches to be up to date before merging
- [ ] Require signed commits
- [ ] Require linear history
- [ ] Restrict who can push to matching branches
- [ ] Do not allow bypassing the above settings

These are **not** in `.github/workflows/ci.yml` because GitHub Actions
can't enforce branch protection — only the repo's web settings can.
Document this in the deployment checklist (next section) so the gap
isn't invisible.

---

## Pre-deployment compliance checklist

What the operator must complete before the steward goes live in a
regulated environment, in addition to anything in the standard Zepz
deployment checklist:

- [ ] Branch protection rules enabled on `main` (see above)
- [ ] `MCP_BEARER_TOKEN` set to a 32+ byte random value, stored in
  the secret store (Vault / AWS SM / k8s Secret), rotated per
  Zepz token-rotation policy
- [ ] `MCP_BEARER_TOKEN_NAME` set to a value that identifies the
  intended single principal (or a meaningfully-shared service ID)
- [ ] `DATABRICKS_TOKEN` is a service-principal token (not a
  personal PAT), with grants narrowed to the catalogs the steward
  is intended to read
- [ ] Per-tool sub-credentials (`MCP_TOOL_TOKEN_<TOOL>`) provisioned
  for the cost-bearing tools (`execute_sql_safe` at minimum)
- [ ] Dedicated SQL warehouse provisioned for the steward; warehouse
  ID set in `MCP_DATABRICKS_WAREHOUSE_ID`; warehouse has no grants
  on PII / payments / KYC catalogs
- [ ] Network egress allow-list at the infra layer permits only the
  Databricks workspace host and (optionally) the OTel collector
- [ ] `MCP_AUDIT_LOG_PATH` points at a persistent volume backed by
  the log shipper of choice; retention configured per Zepz audit
  policy (typically 1 year+)
- [ ] Prometheus scrape configured against `/metrics`; alerts wired
  per `RUNBOOK.md` "Suggested alerts"
- [ ] `OTEL_EXPORTER_OTLP_ENDPOINT` points at the production
  collector; service name registered in the trace catalog
- [ ] Incident-response playbook ([`INCIDENT_RESPONSE.md`](INCIDENT_RESPONSE.md))
  reviewed with the on-call team; communication template populated
  with Zepz IR channel
- [ ] First scheduled credential-rotation drill completed (revoke +
  rotate + verify under controlled conditions)
- [ ] Penetration test scheduled (deferred — not a blocker for
  internal-only deployment, required for any external exposure)

---

## Gaps documented honestly

Things a strict CISO will note, with the agreed mitigation:

| Gap | Risk | Mitigation |
|---|---|---|
| No formal SOC 2 attestation | Insufficient for customer audit demands | Schedule a Type I audit once production is stable for 60 days |
| No formal ISO 27001 certification | Same | Same — typically follows SOC 2 |
| No pen test on record | Unknown unknowns | Engage Zepz's preferred tester before any external exposure |
| No DR plan | Stateless service can't lose data, but the *availability* SLO is undefined | Codify in Zepz's standard service catalog format |
| Per-end-user identity gap | Multi-tenant deployments cannot distinguish humans behind a shared bearer token | Either OAuth-per-user flow or X-End-User-header propagation — see [`SECURITY.md`](SECURITY.md) known-limitation #3 |
| No FedRAMP / HIPAA / PCI-DSS scope decisions | Unknown what the steward is permitted to handle | Tied to data-classification decision (see [`SECURITY.md`](SECURITY.md)) |

---

## See also

- [`SECURITY.md`](SECURITY.md) — threat model and known limitations
- [`INCIDENT_RESPONSE.md`](INCIDENT_RESPONSE.md) — five compromise
  scenarios with detect/contain/eradicate/recover/lessons
- [`RUNBOOK.md`](RUNBOOK.md) — operational guide for on-call
- [`STRESS_FINDINGS.md`](STRESS_FINDINGS.md) — empirical reliability
  characteristics
- [`COMPATIBILITY.md`](COMPATIBILITY.md) — verified client matrix
