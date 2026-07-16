# LiteLLM fork upgrade + deploy — runbook

Exact procedure to rebase this fork onto upstream `BerriAI/litellm` and ship a new gateway
image. Commands assume you run them from the repo root on the operator workstation, with
`gcloud` authenticated and Docker running.

> Deploy is normally automated. `.github/workflows/deploy-gateway.yml` builds the image on a
> push to `main` (image-affecting paths) and, after manual approval in the `production` GitHub
> Environment, runs the build+deploy over IAP SSH (keyless via WIF). The rebase (sections 1-5)
> is still done here on the workstation; the force-push in section 5 triggers the pipeline, which
> automates sections 6-9. The commands in sections 6-9 remain the manual fallback and the
> source-of-truth for what the workflow does (canary, rollback tag, health check are the same).

## 0. Deployment variables

These are intentionally not committed with real values (public repo). Set them from the
operator's private notes / Claude project memory before running anything:

```bash
VM=<compute-instance-name>          # the gateway VM
ZONE=<gcp-zone>                     # its zone
IMAGE=<registry>/<project>/litellm  # e.g. a GCR path; image tags are $IMAGE:custom and $IMAGE:vX.Y.Z
GATEWAY_URL=<https public gateway>  # for post-deploy verification
```

The VM runs `docker compose` with `$IMAGE:custom` and a bind-mounted `config.yaml` on
port 4000. `config.yaml` and every secret (`LITELLM_MASTER_KEY`, provider keys) live only on
the VM. Never print or commit them.

## 1. Assess the delta

```bash
git remote -v                         # confirm origin=upstream, fork=the fork
git fetch origin
echo "ours:     $(grep -m1 '^version' pyproject.toml)"
echo "upstream: $(git show origin/main:pyproject.toml | grep -m1 '^version')"
git rev-list --count main..origin/main            # how many upstream commits are new
git log --oneline origin/main..main               # the fork's local commits (dynamic SHAs)
```

Classify each commit from the last command: backend/deploy customization = keep, UI = drop.

## 2. Back up and reset onto upstream

```bash
git branch backup/pre-upgrade-$(git show origin/main:pyproject.toml | grep -m1 '^version' | grep -oE '[0-9]+\.[0-9]+\.[0-9]+') main
git reset --hard origin/main
```

## 3. Re-apply the keep-commits (drop UI)

Interactive rebase is not available in the automated shell, so cherry-pick the keep-commits by
SHA in chronological order (oldest first). Get the SHAs from step 1's log; do not include UI
commits.

```bash
git cherry-pick <oldest-keep-sha> <next-keep-sha> ...
# on conflict: resolve (premium_user is the usual one), then:
#   git add -A && git cherry-pick --continue
# if a picked commit is UI and turns empty after taking upstream, prefer to have skipped it
```

Verify the customizations survived:

```bash
grep -nE 'premium_user: bool = True' litellm/proxy/proxy_server.py
grep -nE 'config.yaml|STORE_MODEL_IN_DB' docker-compose.yml
git log --oneline -1 -- 'ui/litellm-dashboard/src/app/(dashboard)/layout.tsx'  # should be an upstream commit
python3 -c "import ast; ast.parse(open('litellm/proxy/proxy_server.py').read()); print('parses')"
```

## 4. Lint / format

```bash
make pre-commit    # only runs on staged changes; fix anything it flags, restage, re-run
```

## 5. Push the fork

History was rewritten, so force-push `main` to the fork remote:

```bash
git push fork main --force
```

## 6. Build and push the image

Cross-platform amd64 build (slow Python stage under emulation, native UI stage):

```bash
docker buildx build --platform linux/amd64 \
  -t $IMAGE:custom \
  -t $IMAGE:v<upstream-version> \
  --push .
```

## 7. Deploy on the VM (with rollback safety)

```bash
# free disk if tight (safe: never removes the running image or volumes)
gcloud compute ssh $VM --zone=$ZONE --command='df -h /; docker image prune -f; docker builder prune -f; df -h /'

# snapshot current prod image for rollback
gcloud compute ssh $VM --zone=$ZONE --command="docker tag $IMAGE:custom $IMAGE:rollback"

# pull the new image and restart just the litellm service
gcloud compute ssh $VM --zone=$ZONE --command='cd ~ && docker compose pull litellm && docker compose up -d litellm'
```

## 8. Verify (real, not mocks)

```bash
# health
gcloud compute ssh $VM --zone=$ZONE --command='curl -s -o /dev/null -w "health %{http_code}\n" http://localhost:4000/health/liveliness'

# a real model completion with the master key (do not print the key)
gcloud compute ssh $VM --zone=$ZONE --command='K=$(python3 -c "print([l.split(chr(61),1)[1].strip().strip(chr(34)).strip(chr(39)) for l in open(\"/root/.env\") if l.startswith(\"LITELLM_MASTER_KEY\")][0])"); curl -s -H "Authorization: Bearer $K" -H "Content-Type: application/json" http://localhost:4000/v1/chat/completions -d "{\"model\":\"<a-cheap-model>\",\"messages\":[{\"role\":\"user\",\"content\":\"say ok\"}],\"max_tokens\":10}" | head -c 400'
```

Adjust the `.env` path and a known-cheap model name to the actual deployment. Also confirm from
a client that Chat and the MCP connector still work if those are in use.

## 9. Rollback if broken

```bash
gcloud compute ssh $VM --zone=$ZONE --command="docker tag $IMAGE:rollback $IMAGE:custom && cd ~ && docker compose up -d litellm"
```

Then investigate the failure before retrying. The upstream jump can be hundreds of commits, so a
regression is plausible; the rollback tag makes reverting a single command.

## Config compatibility across versions (validate before restarting prod)

An upstream jump can tighten `config.yaml` validation, and the proxy refuses to start on an invalid
config, which is a hard outage if you find out by restarting prod. Before step 7, dry-run the new
image against the live `config.yaml` in a throwaway container (dummy DB so it never touches prod):

```bash
gcloud compute ssh $VM --zone=$ZONE --command='docker run -d --name litellm-canary -v ~/config.yaml:/app/config.yaml:ro --env-file ~/.env -e DATABASE_URL="postgresql://none:none@127.0.0.1:1/none" '$IMAGE':v<ver> --config /app/config.yaml; sleep 25; docker inspect litellm-canary --format "running={{.State.Running}} exit={{.State.ExitCode}}"; docker logs litellm-canary 2>&1 | grep -iE "Invalid config|ValueError|startup failed" | tail; docker rm -f litellm-canary'
```

`running=true` (vs `exit!=0`) means it passed config validation; a crash prints the exact key to fix.
Known example: 1.93.0 made `oauth2_flow` required on any `mcp_servers` entry with `auth_type: oauth2`
(values `client_credentials` or `authorization_code`); the 1.92 to 1.93 upgrade crashed on startup
until `config.yaml`'s `avenia` MCP server got `oauth2_flow: authorization_code` (per-user browser OAuth).
Fix `config.yaml` on the VM, re-run the canary, then deploy.

## Notes

- `config.yaml` on the VM (models, aliases, MCP servers) is never touched by the image; it is the
  source of truth for model list and is mounted at runtime
- Do not delete the `backup/pre-upgrade-*` branch until the deploy is confirmed healthy for a while
- If `make pre-commit` regenerates dashboard API types, stage `schema.d.ts`, re-run, then commit
- Keep the disk trend in mind: SpendLogs growth is the usual reason the VM fills; retention/archival
  is a separate task worth doing so deploys do not get blocked on disk
