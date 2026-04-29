# databricks-ai-steward (Helm chart)

Helm chart for the Databricks AI Steward MCP server. Deploys the
`streamable-http` transport behind a `ClusterIP` service with
`/healthz`, `/readyz`, and (optionally) `/metrics`. Probes are
unauthenticated; the API surface (`/mcp`) requires a bearer token
when `secrets.bearer.secretName` is set.

## Quick start

```bash
# 1. Provision workspace credentials as a Secret. The Secret must
#    contain `host` and `token` keys.
kubectl create secret generic dbx-creds \
  --from-literal=host=https://myworkspace.cloud.databricks.com \
  --from-literal=token=dapi...

# 2. (Recommended) Provision a bearer token for HTTP auth.
kubectl create secret generic steward-bearer \
  --from-literal=token=$(openssl rand -hex 32)

# 3. Install.
helm install steward ./deploy/helm/databricks-ai-steward \
  --set secrets.databricks.secretName=dbx-creds \
  --set secrets.bearer.secretName=steward-bearer \
  --set networkPolicy.databricks.host=myworkspace.cloud.databricks.com
```

## Common overrides

### Per-end-user identity (oauth2-proxy / authelia / istio)

```yaml
config:
  trustEndUserHeader: "1"
  endUserHeader: X-Forwarded-User       # or Remote-User, X-Auth-Request-Email
  bearerTokenName: team-data            # fallback when header is absent
```

The upstream proxy MUST strip any client-supplied value of the chosen
header before setting its own. See `RUNBOOK.md` "Per-end-user
attribution" for the full ingress recipe.

### Per-tool credentials (least privilege)

```yaml
secrets:
  perTool:
    - tool: execute_sql_safe
      secretName: steward-tool-token-sql
      tokenKey: token
    - tool: list_catalogs
      secretName: steward-tool-token-catalogs
      tokenKey: token
```

Each tool resolves its own service principal at runtime — see
`SECURITY.md` "Per-tool credentials" and `RUNBOOK.md` "Rotating
credentials".

### Prometheus + OpenTelemetry

```yaml
config:
  prometheusEnabled: "1"
  otelExporterOtlpEndpoint: http://otel-collector.observability:4317
serviceMonitor:
  enabled: true        # requires prometheus-operator CRDs in cluster
```

### Log shipper sidecar (audit log → immutable storage)

The audit log is tamper-evident in-app via a hash chain (verify with
`python -m mcp_server.audit_verify`). For long-term retention pair
that with a sidecar that ships records to S3 + Object Lock or
equivalent.

```yaml
logShipper:
  enabled: true
  image: timberio/vector:0.34.0-distroless-static
  args: ["--config", "/etc/vector/vector.yaml"]
  # Mount your config via a ConfigMap volume (not shown).
```

## Values reference

See `values.yaml` — every key is annotated. The most-used ones:

| Key | Default | Purpose |
|---|---|---|
| `replicaCount` | `2` | Replicas (ignored if `autoscaling.enabled`) |
| `image.repository` | `ghcr.io/sherman94062/databricks-ai-steward` | Image to pull |
| `image.tag` | (Chart.AppVersion) | Image tag |
| `service.port` | `8765` | Service port |
| `secrets.databricks.secretName` | `databricks-workspace` | Required Secret name |
| `secrets.bearer.secretName` | `""` | Optional bearer-token Secret |
| `config.trustEndUserHeader` | `""` | Set to `"1"` for per-end-user identity |
| `config.endUserHeader` | `""` | Header name (default `X-End-User`) |
| `config.rateLimit` | `""` | `MCP_RATE_LIMIT` value |
| `networkPolicy.enabled` | `true` | Apply NetworkPolicy (ingress + egress) |
| `networkPolicy.databricks.host` | `""` | Workspace FQDN for egress narrowing |
| `podDisruptionBudget.enabled` | `true` | Apply PDB (`minAvailable: 1`) |
| `autoscaling.enabled` | `false` | Apply HPA (CPU-target) |
| `serviceMonitor.enabled` | `false` | Prometheus Operator integration |
| `logShipper.enabled` | `false` | Run an audit-log shipper sidecar |

## Verifying a render

```bash
helm lint ./deploy/helm/databricks-ai-steward
helm template steward ./deploy/helm/databricks-ai-steward -f my-values.yaml
```

CI runs `helm lint` on every change; see `.github/workflows/ci.yml`.
