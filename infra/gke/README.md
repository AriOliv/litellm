# infra/gke

Infrastructure-as-code for running the LiteLLM gateway on GKE Autopilot with Cloud SQL. See
`docs/gke-migration/runbook.md` for the full migration story, verification, and cutover/rollback.
This directory carries no project-specific values; fill them from your private notes.

## Layout

- `terraform/` provisions the cluster, Cloud SQL (HA), Artifact Registry, a global static IP, the
  Workload Identity service account, and IAM. Copy `terraform.tfvars.example` to `terraform.tfvars`
  and fill it (do not commit `terraform.tfvars`).
- `k8s/` holds the workload manifests. Values are `${PLACEHOLDER}` tokens resolved with `envsubst`.

## Order of operations

1. `cd terraform && terraform init && terraform plan -out tfplan && terraform apply tfplan`
   Note the outputs: cluster name, static IP, Cloud SQL connection name, gateway GSA email.
2. `gcloud container clusters get-credentials <cluster_name> --region <region>`
3. Load secrets into Secret Manager and create the config ConfigMap (runbook Phase 2), then migrate
   the database (Phase 3).
4. Render and apply the manifests with the placeholders filled:

   ```
   export APP_NAMESPACE=litellm GATEWAY_KSA=litellm
   export PROJECT_ID=... GATEWAY_DOMAIN=... IMAGE=...
   export CLOUDSQL_CONNECTION_NAME=... GSA_EMAIL=... STATIC_IP_NAME=... CERT_MAP_NAME=...
   kubectl kustomize k8s | envsubst | kubectl apply -f -
   kubectl -n "$APP_NAMESPACE" rollout status deploy/litellm
   ```

5. Validate against the load balancer IP before touching DNS (runbook Verification), then cut over.

## Placeholders

| Token | Meaning |
| --- | --- |
| `PROJECT_ID` | GCP project id |
| `APP_NAMESPACE` | Kubernetes namespace (default `litellm`) |
| `GATEWAY_KSA` | Kubernetes service account name (default `litellm`) |
| `GSA_EMAIL` | Google service account email (Terraform output `gateway_gsa_email`) |
| `IMAGE` | Full gateway image reference (e.g. the immutable `:git-<sha>` tag) |
| `CLOUDSQL_CONNECTION_NAME` | Terraform output `cloudsql_connection_name` |
| `GATEWAY_DOMAIN` | Public gateway hostname |
| `STATIC_IP_NAME` | Name of the reserved global static IP |
| `CERT_MAP_NAME` | Certificate Manager cert map holding the managed cert for the domain |

## Notes

- The Secret Manager CSI driver and the config ConfigMap deliver secrets and `config.yaml`; neither
  is committed here. `secretproviderclass.yaml` lists a representative subset of secrets; extend it to
  the full `.env`.
- `GCPBackendPolicy` sets a 3600s backend timeout so streaming responses are not cut off.
- Keep the gateway domain stable so Google OAuth (SSO) redirect URIs and `PROXY_BASE_URL` need no
  change; only the DNS A record flips at cutover.
