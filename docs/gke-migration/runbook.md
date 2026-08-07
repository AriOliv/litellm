# Gateway migration: single VM (Docker Compose) -> GKE Autopilot + Cloud SQL

This runbook migrates the LiteLLM gateway off the single Compute Engine VM (Docker Compose) onto
GKE Autopilot with a managed Cloud SQL database. It is scoped to the gateway only; the other
services that currently share the VM (LAP, decocms, evolution, the MCP servers) stay where they are
for now.

Deployment specifics that must not live in this public repo (project id, VM name, zone, the public
gateway domain, WIF provider, image path, DNS zone) are read from the operator's private notes and
passed as Terraform variables or repo variables. Secrets (master key, provider keys, DB password,
OAuth client secret) live only in Secret Manager and on the VM's `.env`; never commit or print them.

## Why

The gateway ran on one VM with a root filesystem that trended toward full (SpendLogs growth). When
it filled, it OOM-killed the reverse proxy (a multi-day silent public outage) and then wedged the
guest OS. That model has no autoscaling, no HA, and a disk that has to be babysat. GKE Autopilot
plus Cloud SQL gives horizontal autoscaling, multi-zone HA, and storage that grows on its own, so
the disk failure mode goes away entirely.

## Target architecture

Everything stays in one region (`southamerica-east1`) for latency and BR data residency.

- GKE Autopilot regional cluster. No node pools to size; billing is per-pod; nodes autoscale.
- LiteLLM `Deployment`: >=2 warm replicas across zones, a `HorizontalPodAutoscaler`, a
  `PodDisruptionBudget` keeping >=1 available, readiness/liveness probes on `/health/readiness` and
  `/health/liveliness`.
- Database on Cloud SQL for PostgreSQL 16, regional HA, storage autogrow. The pod reaches it through
  a Cloud SQL Auth Proxy sidecar authenticated by Workload Identity (no static DB key on the wire);
  `DATABASE_URL` points at `127.0.0.1:5432`.
- `config.yaml` is delivered as a `ConfigMap` mounted at `/app/config.yaml`, so models, MCP servers,
  and `general_settings` stay editable without rebuilding the image. `store_model_in_db: true` stays
  on, so the database also holds models; the config and the DB together define the deployment.
- Secrets come from GCP Secret Manager, surfaced into the pod via the Secret Manager CSI driver and
  Workload Identity. No secret is written into a manifest.
- Ingress and TLS via the GKE Gateway API: a global external HTTPS load balancer on a reserved
  static IP, with a Certificate Manager managed certificate for the gateway domain. A
  `GCPBackendPolicy` raises the backend timeout well above the 30s default so LLM streaming (SSE)
  and long completions are not cut off.
- The gateway domain does not change, so Google OAuth (SSO) redirect URIs and `PROXY_BASE_URL` need
  no reconfiguration; only the DNS A record flips at cutover.

## Prerequisites

- `gcloud` authenticated with rights to create GKE, Cloud SQL, Secret Manager, Certificate Manager,
  networking, and IAM in the target project.
- `kubectl`, `terraform`, and the `gke-gcloud-auth-plugin` installed.
- The current gateway `config.yaml` and `.env` from the VM (read-only copies; keep them off the repo).
- A DNS zone you control for the gateway domain, with the ability to lower the record TTL ahead of
  cutover.

## Phase 1 - Provision (Terraform, no production impact)

`infra/gke/terraform` provisions the cluster, Cloud SQL, Artifact Registry, the static IP, the
Workload Identity service accounts, and the IAM bindings. Fill `terraform.tfvars` from your private
notes (see `terraform.tfvars.example`), then:

```
cd infra/gke/terraform
terraform init
terraform plan -out tfplan     # review every created resource and the monthly cost
terraform apply tfplan
```

This spends money (an Autopilot cluster, a Cloud SQL HA instance, a load balancer, a static IP), but
touches nothing the live VM serves. Cluster creation is ~10 min and Cloud SQL HA ~15 min.

## Phase 2 - Secrets and config

1. Load the gateway's environment into Secret Manager, one secret per sensitive value (master key,
   each provider key, the OAuth client secret, Langfuse keys, the Cloud SQL DB password). Do this
   from the VM copy of `.env` without echoing values, e.g. loop over the file and pipe each value
   into `gcloud secrets create/versions add` via stdin. Non-secret env (base URLs, favicon,
   `ALLOWED_EMAIL_DOMAINS`) can stay in the `ConfigMap`/Deployment env.
2. Turn `config.yaml` into a `ConfigMap`: `kubectl create configmap litellm-config
   --from-file=config.yaml=./config.yaml -o yaml --dry-run=client`. Keep the real file out of git.

## Phase 3 - Database migration

Two options; pick per acceptable downtime (see the open decision in the plan):

- Simple (short maintenance window): `pg_dump` the 22 GB `litellm` DB from the VM's `litellm_db`
  container, restore into Cloud SQL, then point the gateway at Cloud SQL. Test the restore into a
  throwaway DB first and time it.
- Near-zero downtime: Database Migration Service continuous replication from the VM Postgres into
  Cloud SQL, then promote at cutover.

Because `store_model_in_db: true`, the DB carries the model list; the migration must be complete and
consistent before the gateway serves production from Cloud SQL.

## Phase 4 - Deploy to the cluster (still no production traffic)

```
# config against the target image first (schema can tighten across versions, e.g. oauth2_flow):
# run the new image with the live config and a dummy DB URL; it must start clean.
kubectl apply -k infra/gke/k8s     # namespace, SA, ConfigMap ref, Deployment, Service, HPA, PDB,
                                   # SecretProviderClass, Gateway, HTTPRoute, GCPBackendPolicy
kubectl -n litellm rollout status deploy/litellm
```

Validate against the load balancer IP directly (a `Host:` header for the gateway domain, or a
temporary DNS name), before any real DNS change. See Verification.

## Phase 5 - Cutover (the only production-affecting step)

1. Lower the gateway domain DNS TTL a day ahead.
2. Do the final DB sync (DMS promote, or the maintenance-window dump/restore).
3. Flip the gateway domain A record from the VM IP to the reserved load balancer IP.
4. Watch: health, real completions, SSO, MCP, streaming, and the HPA under load.

Blue-green: keep the VM gateway container and its DB running and untouched through the soak, so
rollback is just flipping DNS back. Do not delete any VM data.

## Phase 6 - CI/CD

Repoint `.github/workflows/deploy-gateway.yml`: the build job (WIF, immutable image tags) stays; the
deploy job changes from an IAP-SSH `docker compose up` to `kubectl set image` / `kubectl rollout`.
Keep the pieces that already work: the canary that validates the live config against the new image
with a dummy DB, the health gate, the rollback (`kubectl rollout undo`), and the manual approval via
the `production` environment.

## Verification (end to end, real spend)

Pre-cutover, against the load balancer IP:

- `curl -H 'Host: <gateway-domain>' https://<LB_IP>/health/liveliness` returns 200.
- A real `/v1/chat/completions` with a cheap model returns content (costs real money).
- The same call with `"stream": true` streams tokens (proves the raised backend timeout / SSE).
- SSO login completes in a browser.
- An MCP server with `auth_type: oauth2` / `oauth2_flow` connects.
- `/v1/models` lists the models that live in the DB (proves the DB migration).

Post-cutover, against the gateway domain: repeat the above, then a small load test to watch the HPA
add replicas, kill a pod to confirm the PDB keeps >=1 serving, and trigger a Cloud SQL failover to
confirm HA.

## Rollback

Flip the gateway domain DNS back to the VM IP. The VM gateway and DB stayed running and unchanged
through the soak, so service is restored immediately. Investigate, fix, and retry the cutover.

## Cost

Rough monthly, production-grade: two warm Autopilot pods (~2 vCPU / 4Gi each), a Cloud SQL HA
instance (a small custom tier, doubled for HA), the load balancer, and the static IP. On the order
of a few hundred USD per month; more than one small VM, in exchange for HA, autoscaling, and no disk
operations. Refine with real sizing from observed CPU/memory before committing.

## Out of scope (v2)

Moving the other VM services (LAP, evolution, decocms, the MCP servers) into the cluster and turning
the VM off; a Cloud SQL read replica for read-only queries; multi-region.
