# Claude Code LiteLLM Gateway Session History

This document reconstructs Claude Code session `020043eb-61b4-4313-8975-bf71c45c75b2`. It is a historical handoff for agents maintaining the gateway. It distinguishes the original VM work from the current GKE deployment so historical commands are not mistaken for current operations.

## Outcome

The session made Claude Code select non-Claude LiteLLM chat models through aliases, preserved Claude Max OAuth pass-through, added selected models and pricing, enabled UI-managed proxy configuration without moving models or MCP definitions into the database, restored Langfuse tracing, and began but did not deploy an upstream LiteLLM upgrade.

The current gateway configuration was later captured as GKE IaC in `deploy/gke/litellm-config.yaml`. Treat that manifest, not the old VM file, as the active configuration source.

## Timeline

### 2026-06-08: Model selection diagnosis

The user wanted Claude Code to select models exposed by LiteLLM. Gateway discovery was enabled, and the proxy catalog contained both Claude-named and non-Claude models.

Research confirmed the key product constraint: Claude Code's `/model` gateway picker includes only discovered model IDs beginning with `claude` or `anthropic`. It silently excludes names such as GPT, Gemini, Grok, GLM, and Llama even when LiteLLM returns them from its model endpoint.

The accepted design was to expose chat-capable non-Claude groups through `claude-*` or `anthropic-*` LiteLLM aliases. This was preferred over modifying Claude Code or duplicating every provider deployment. Non-chat groups were intentionally excluded.

### 2026-06-09: Alias deployment and OAuth correction

The production LiteLLM configuration then lived on a GCP VM. The session discovered that the local config was stale and correctly downloaded and edited the VM configuration instead of overwriting production from the local copy.

The deployment added aliases through `router_settings.model_group_alias` while leaving the original `model_list`, fallbacks, MCP servers, and top-level settings unchanged. Model discovery and real inference through non-Claude aliases were validated after the LiteLLM container restart.

An initial attempt to set `ANTHROPIC_AUTH_TOKEN` in Claude Code was incorrect. It replaced the user's Claude Max OAuth bearer token, so LiteLLM could not forward subscription authentication to Anthropic. The corrected design is:

```text
Claude Code -> LiteLLM custom authentication header -> LiteLLM
Claude Code OAuth Authorization header -> LiteLLM -> Anthropic for Claude models
```

The client must use a separate LiteLLM custom header for gateway authentication and must not use proxy `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY` when Claude Max pass-through is expected. LiteLLM requires `forward_client_headers_to_llm_api: true` for this route.

### 2026-06-09: Claude Fable 5

`claude-fable-5` was added with recorded pricing of $10 per million input tokens and $50 per million output tokens. It appeared in the catalog after deployment.

The session also observed that direct Anthropic traffic without either an upstream API key or forwarded OAuth fails. This was expected under the selected OAuth pass-through design, not a reason to add a static Anthropic secret to clients.

### 2026-06-12: UI configuration and Gemini 3.5 Flash

The LiteLLM UI reported that auto-router creation required model storage in the database. The adopted configuration enabled `store_model_in_db: true` but limited `supported_db_objects` to UI-managed resources. It explicitly excluded `models` and `mcp`, retaining YAML as their source of truth.

`gemini-3.5-flash` was added with input, output/reasoning, and cache-read pricing. A corresponding Claude Code discovery alias was added and exercised successfully through the Anthropic-compatible messages endpoint.

### 2026-06: Langfuse recovery and isolation

Langfuse callbacks were already present but initially did not authenticate correctly. Updating a Compose `.env` file followed only by `docker restart` did not refresh environment values because the values were captured when the container was created.

The successful fix recreated the LiteLLM service. Health was then validated through LiteLLM's Langfuse integration and by observing a gateway-generated trace. Gateway traces were isolated using dedicated environment and release labels, rather than shared application labels.

Operational rule: environment changes in Compose require a service recreation, not merely a container restart.

### 2026-06-18: Cancelled upstream upgrade

The source tree was rebased onto upstream and preserved local changes. A local Docker image was built and pushed, but it was built on Apple Silicon as ARM64. The production VM was AMD64, so image pull failed with no matching platform manifest.

Production was not changed. The user canceled the upgrade before rollout.

This incident established the image-platform rule: publish `linux/amd64` or multi-architecture images and validate them before promotion. The repository's VM deployment workflow now uses Buildx with `platforms: linux/amd64`, immutable tags, a canary using the live config, a health check, and a real completion before promotion.

### 2026-09: GKE consolidation

The gateway was consolidated onto GKE. `deploy/gke/litellm-config.yaml` and `deploy/gke/redis.yaml` were added as IaC. Later reconciliation restored production-only settings that had drifted from the manifest, including restricted expensive models, Azure fallback, and 1M context aliases.

The current gateway uses GKE, namespace `litellm`, with Redis and Cloud SQL. The original VM process is historical context only.

## Decisions That Must Remain Intact

| Decision | Rationale | Consequence |
| --- | --- | --- |
| Alias non-Claude chat groups with a `claude` or `anthropic` prefix | Claude Code filters gateway discovery by prefix | Add an alias whenever a new non-Claude chat model should be selectable in `/model` |
| Keep Claude Max OAuth separate from LiteLLM authentication | A proxy `ANTHROPIC_AUTH_TOKEN` replaces Claude's OAuth bearer header | Use a custom LiteLLM authentication header and forward client headers only where appropriate |
| Keep models and MCP in YAML | They are reviewed, versioned operational configuration | Do not add `models` or `mcp` to `supported_db_objects` |
| Use team-scoped deployments for expensive models | Access groups can be bypassed by broad key configuration | Confirm team membership and fallback visibility before changing a costly deployment |
| Recreate on environment changes | Compose supplies environment at container creation | Restart alone does not load changed environment values |
| Build platform-specific gateway images | ARM64 images cannot run on AMD64 infrastructure | Use `linux/amd64` builds and immutable image tags |

## Current Configuration Landmarks

- Model definitions and pricing: `deploy/gke/litellm-config.yaml` under `model_list`
- Claude Code aliases and `[1m]` aliases: `router_settings.model_group_alias`
- Fallback routing: `router_settings.fallbacks`
- Redis and Langfuse callbacks: `litellm_settings`
- Database-backed UI resources and OAuth header forwarding: `general_settings`
- GKE apply instructions: `deploy/gke/README.md`
- GKE agent instructions: `deploy/gke/AGENTS.md`

## Follow-Up Checklist For Agents

1. Read `deploy/gke/AGENTS.md` and inspect the current manifest before proposing a gateway change
2. Decide whether the target is a chat model usable in Claude Code; only then add a discovery alias
3. Preserve team scoping, fallbacks, and source-of-truth boundaries unless the requested behavior requires a deliberate change
4. Apply the ConfigMap from the versioned YAML, restart the GKE deployment, and wait for rollout completion
5. Validate the live catalog and make one inexpensive real request using the intended API surface
6. Confirm telemetry after observability-related changes without logging credentials or request payloads
7. Reconcile any emergency cluster change into Git immediately

## Historical Artifacts

The original transcript is stored outside the repository in the Claude session archive under the session ID named above. Historical Git artifacts still include a pre-upgrade backup branch and a generated-artifact stash. They are retained as recovery context, not as a deployment procedure.
