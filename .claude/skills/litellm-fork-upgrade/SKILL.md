---
name: litellm-fork-upgrade
description: >-
  Update this LiteLLM fork from the upstream BerriAI/litellm main branch, keeping the
  fork's customizations, then rebuild the container image and deploy it to the running
  gateway. Use whenever someone asks to update/upgrade LiteLLM from upstream, sync the
  fork, pull the latest, or ship a new gateway image. Covers the rebase strategy (which
  commits to keep vs drop), the image build, the deploy, verification and rollback.
---

# LiteLLM fork upgrade + deploy

This fork tracks `BerriAI/litellm` and carries a small set of local customizations on top.
Upgrading means rebasing those customizations onto the newest upstream `main`, rebuilding
the image, and deploying it to the gateway VM. Follow `references/runbook.md` for the exact
commands; this file is the map.

## Ground truth to establish first (do not assume)

The commit SHAs of the fork's customizations change on every rebase, so never hardcode them.
Discover them live:

```bash
git remote -v                      # origin should be BerriAI/litellm (upstream), fork should be the fork
git fetch origin
git log --oneline origin/main..main   # these are the fork's local commits to reason about
```

Deployment specifics (VM name, GCP zone, GCR image tag, public gateway URL) are intentionally
not written into this public repo. Read them from the operator's private notes / Claude memory
for this project before deploying. Secrets (master keys, `.env` values) live only on the VM and
must never be printed, committed, or pasted anywhere.

## What to keep vs drop when rebasing

Keep (re-apply on top of upstream): the fork's backend/deploy customizations. As of the last
upgrade these are:

- `premium_user: bool = True` in `litellm/proxy/proxy_server.py` (unlocks enterprise features)
- `docker-compose.yml` using a mounted `config.yaml` (`--config=/app/config.yaml`) with
  `STORE_MODEL_IN_DB` driven by `general_settings.store_model_in_db` in that file, not an env var
- the `.gitignore` negation that tracks `.claude/skills/`, plus this skill itself

Drop (take upstream's version): any UI / dashboard-layout customization. Upstream reworks the
admin UI frequently and our tweaks conflict every time, so let upstream win on `ui/**`. If a
past UI commit shows up in `git log origin/main..main`, do not cherry-pick it.

Before each upgrade, re-read the actual `git log origin/main..main` and classify each commit as
keep or drop by the same principle (backend/deploy = keep, UI = drop), because the set can change.

## Procedure (summary; full commands in references/runbook.md)

1. Assess: `git fetch origin`, compare version (`pyproject.toml`) and count (`git rev-list --count main..origin/main`)
2. Back up the current fork state to a branch, then `git reset --hard origin/main`
3. Cherry-pick the keep-commits in chronological order; resolve conflicts (the `premium_user` line is the usual one). Do not re-apply UI commits
4. `make pre-commit` (per repo CLAUDE.md) and fix anything it flags
5. Force-push the rebased `main` to the fork
6. Build the image for `linux/amd64` with buildx and push to the registry, tagging both `:custom` and a versioned tag
7. Deploy on the VM: free disk if needed, snapshot a rollback tag, pull, restart, verify
8. Verify: `/health/liveliness`, a real model completion, and that Chat + the MCP connector still work
9. Roll back to the snapshot tag if verification fails

## Gotchas

- The VM disk trends toward full (SpendLogs growth). Check `df -h /` before deploying; run a safe
  `docker image prune -f` / `docker builder prune -f` to reclaim space. Do not prune the currently
  running image
- `config.yaml` (models, aliases, MCP) lives mounted on the VM, not baked into the image, so a new
  image never overwrites it
- The image build is a cross-platform build (amd64 from an arm workstation); the Python stage runs
  under emulation and is slow. The UI stage builds natively. Expect a long build
- Keep secrets out of everything you write or print
