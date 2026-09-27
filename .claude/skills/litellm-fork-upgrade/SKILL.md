---
name: litellm-fork-upgrade
description: >-
  Updates this LiteLLM fork from BerriAI/litellm main while preserving the
  fork-owned GKE configuration and automation, then builds, validates, deploys,
  and verifies the gateway. Use whenever someone asks to update, upgrade, sync,
  or deploy the LiteLLM fork.
---

# LiteLLM Fork Upgrade

This fork is rebuilt on top of upstream `main`. Do not merge a large stale fork
history into upstream: that recreates thousands of irrelevant UI conflicts.

Read `references/runbook.md` for exact commands.

## Preserve

- `deploy/gke/`: model configuration, aliases, Redis, and operational guidance
- `.github/workflows/deploy-gateway.yml`: GKE image rollout
- `.github/workflows/upstream_sync.yml`: reviewed upstream reconstruction
- the cross-model security review workflow and script
- this skill and the `.gitignore` rule that allows it to be tracked
- `docker-compose.yml` configuration mounting, where still useful locally

## Take From Upstream

- all `ui/**`
- LiteLLM provider and routing code unless a current production regression proves
  that upstream still lacks a required fix
- Docker runtime dependencies already present upstream
- license evaluation; do not restore a hard-coded premium flag

## Upgrade Sequence

1. Fetch both remotes and record versions and divergence
2. Create a backup branch of the current fork
3. Create a fresh upgrade branch at `origin/main`
4. Reapply the curated fork-owned paths only
5. Validate config, targeted behavior, and repository checks
6. Build an immutable `linux/amd64` image
7. Start a disposable config canary before touching the deployment
8. Roll out to GKE with the previous image recorded for rollback
9. Verify health and real completions on critical routes
10. Push the reviewed upgrade branch, then replace fork `main` only after approval

## Rules

- The active gateway is GKE, namespace `litellm`; VM deployment instructions are historical
- `deploy/gke/litellm-config.yaml` is the source of truth for models and MCP
- Never print or commit Kubernetes secrets
- Use immutable image tags and preserve the previous deployment image
- Take upstream's UI wholesale
- Validate current production config against the candidate image before rollout
