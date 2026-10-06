# LiteLLM Fork Upgrade Runbook

## Check Upstream First

Before editing code or building an image:

1. Search upstream `main` for the bug and its tests
2. Identify the first upstream commit and tag containing the fix
3. Check whether an official image exists for that tag
4. Prefer the official image for canary and deployment

Build a custom image only when the official release lacks the fix or the gateway
requires a verified fork-only source change. Record that reason before building.

This fork always carries `premium_user: bool = True`, which the gateway's SSO
population and the embedded LiteAdmin MCP both need. Patch it in source. When a
required upstream feature is not yet in any official image, build from the root
`Dockerfile`; otherwise an official image with the patch applied is acceptable.

## Assess

```bash
git fetch origin main
git fetch fork main
git rev-list --count fork/main..origin/main
git rev-list --count origin/main..fork/main
git log --oneline origin/main..fork/main
```

Confirm `origin` is BerriAI and `fork` is the writable fork. Read the current
repository and GKE memories before deciding which commits remain relevant.

## Reconstruct

Create a backup branch, then create a new branch directly from upstream:

```bash
git branch backup/pre-upstream-<version>-<date> fork/main
git switch -c litellm_upstream_<version> origin/main
```

Reapply only fork-owned operational paths. Take upstream's `ui/**`, provider
implementations, model registry, Docker dependencies, and license checks.

The automated workflow uses the same principle: it creates a binary patch for
the curated fork paths, resets its sync branch to upstream, and applies that
patch. A conflict means the curated path list or customization needs human
review; do not hand hundreds of UI conflicts to an agent.

## Validate Source

Run focused tests for every fork behavior and the repository checks required by
`CLAUDE.md`. At minimum:

```bash
git diff --check
make check
```

For provider behavior, use the mapped tests under `tests/unit/` and then perform
the same request against a live candidate.

## Build

Build an immutable Linux AMD64 image. The GitHub workflow uses Buildx; a manual
Cloud Build must enable BuildKit because the Dockerfile uses cache mounts.

```bash
docker buildx build --platform linux/amd64 --provenance=false \
  -t "$IMAGE:git-$SHA" --push .
```

Do not promote a mutable tag before validation.

## Canary

Start a disposable pod in the `litellm` namespace with:

- the candidate image
- the live `litellm-config` ConfigMap mounted at `/app/config.yaml`
- `litellm-secrets` through `envFrom`
- an intentionally invalid `DATABASE_URL` so it cannot migrate production

The pod should remain running long enough to prove configuration parsing and
application startup. Inspect logs for schema, import, and startup failures, then
delete it.

## Deploy And Roll Back

Record the current image, set the candidate image, and wait for rollout:

```bash
previous="$(kubectl -n litellm get deployment litellm -o jsonpath='{.spec.template.spec.containers[?(@.name=="litellm")].image}')"
kubectl -n litellm set image deployment/litellm "litellm=$IMAGE"
kubectl -n litellm rollout status deployment/litellm --timeout=10m
```

If rollout or verification fails:

```bash
kubectl -n litellm set image deployment/litellm "litellm=$previous"
kubectl -n litellm rollout status deployment/litellm --timeout=10m
```

## Verify

Validate all of the following without printing credentials:

1. `2/2` replicas are ready
2. `/health/liveliness` returns HTTP 200
3. `/v1/models` includes the expected model groups for the caller
4. An inexpensive normal completion succeeds
5. GPT reasoning models work with `max_tokens` and function tools
6. Claude Code aliases, including `[1m]`, resolve correctly
7. MCP and Langfuse still initialize when changed by the upgrade

## Publish

Push the upgrade branch to `fork` and review its CI. Replacing `fork/main`
rewrites history by design, so retain the backup branch and use
`--force-with-lease`, never an unqualified force push. Production deployment is
approval-gated by the GitHub `production` environment.
