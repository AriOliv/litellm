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

- `.github/workflows/deploy-gateway.yml`: GKE image rollout, with every cluster,
  registry and URL value read from repository variables
- `.github/workflows/upstream_sync.yml`: reviewed upstream reconstruction
- the cross-model security review workflow, its script and its hash-pinned requirements
- this skill and the `.gitignore` rule that allows it to be tracked
- the `premium_user: bool = True` SSO bypass required by this gateway
- fork code fixes upstream still lacks; re-check each against upstream before carrying it

## Never Commit Here

This fork is public. Gateway configuration (`deploy/gke/`, model lists, aliases,
team IDs, internal endpoints), cluster coordinates and session handoff notes stay
out of it. They live in the operator's private configuration and memory.

## Take From Upstream

- all `ui/**`
- LiteLLM provider and routing code unless a current production regression proves
  that upstream still lacks a required fix
- Docker runtime dependencies already present upstream
- all other license and enterprise behavior outside the required SSO bypass

## Upgrade Sequence

1. Check upstream source, tags, and official images for the requested fix
2. Prefer an official image when it already contains the fix
3. Fetch both remotes and record versions and divergence
4. Create a backup branch of the current fork
5. Create a fresh upgrade branch at `origin/main`
6. Reapply the curated fork-owned paths only
7. Validate config, targeted behavior, and repository checks
8. Build a custom image only when fork-only code remains necessary
9. Start a disposable config canary before touching the deployment
10. Roll out to GKE with the previous image recorded for rollback
11. Verify health and real completions on critical routes
12. Push the reviewed upgrade branch, then replace fork `main` only after approval

## Rules

- The active gateway is GKE, namespace `litellm`; VM deployment instructions are historical
- The live `litellm-config` ConfigMap holds models and MCP; its source of truth is private
- Never print or commit Kubernetes secrets
- Use immutable image tags and preserve the previous deployment image
- Take upstream's UI wholesale
- Never build a custom image before checking whether an official upstream image contains the fix
- Validate current production config against the candidate image before rollout
