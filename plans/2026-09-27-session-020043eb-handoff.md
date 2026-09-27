# Session 020043eb Agent Handoff

## Purpose

Provide a reproducible operating model for the work completed in Claude Code session `020043eb-61b4-4313-8975-bf71c45c75b2`, without carrying forward obsolete VM procedures or sensitive configuration.

## Read Order

1. `deploy/gke/AGENTS.md`
2. `deploy/gke/SESSION-020043EB-HISTORY.md`
3. `deploy/gke/litellm-config.yaml`
4. `deploy/gke/README.md`

## Current Architecture

- LiteLLM runs in GKE namespace `litellm`
- `deploy/gke/litellm-config.yaml` is the reviewed source of truth for proxy behavior
- `deploy/gke/redis.yaml` provides Redis for shared routing coordination and response caching
- Kubernetes secrets hold provider credentials, the LiteLLM key, database credentials, and Redis password
- Cloud SQL stores LiteLLM data; models and MCP definitions intentionally remain YAML-managed

## Change Playbook

### Add or modify a model

1. Add or update the group in `model_list`, with provider route and verified pricing where applicable
2. Decide whether Claude Code must select it
3. If it is a chat-capable non-Claude model, add a `claude-*` or `anthropic-*` alias in `model_group_alias`
4. Do not create aliases for embeddings, transcription, image generation, or video generation
5. If the model is 1M-context selectable in Claude Code, add the literal `[1m]` alias that maps to the ordinary group name
6. Consider authorization before adding it: expensive models use `model_info.team_id`, which also affects fallback visibility
7. Apply the ConfigMap from the file, restart the deployment, wait for rollout, inspect the live model catalog, then make an inexpensive real request

### Configure Claude Code through LiteLLM

1. Point Claude Code to the proxy with `ANTHROPIC_BASE_URL`
2. Enable model discovery with `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`
3. Authenticate to LiteLLM using a separate custom header
4. Do not set proxy `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY` when users rely on Claude Max OAuth pass-through
5. Keep `forward_client_headers_to_llm_api: true` in the gateway configuration for the upstream OAuth flow
6. Select aliases through Claude Code's `/model` picker

### Change UI-managed LiteLLM resources

1. Keep `store_model_in_db: true` when the UI requires stored configuration
2. Preserve the omission of `models` and `mcp` in `supported_db_objects`
3. Use the UI/database only for the supported resource classes
4. Do not migrate YAML models or MCP definitions to the database as incidental cleanup

### Update observability

1. Keep Langfuse callbacks in the LiteLLM configuration
2. Update credentials only through Kubernetes secrets
3. Separate the gateway's traces with its established environment and release labels
4. Verify with a real LiteLLM request and inspect telemetry, without logging prompt content or credentials

## Rejected Approaches

| Approach | Why it was rejected |
| --- | --- |
| Expect Claude Code to list arbitrary model IDs from LiteLLM | Discovery filters model IDs by `claude` or `anthropic` prefix |
| Change Claude Code to support every provider model directly | Gateway aliases solve the integration at the correct boundary |
| Set `ANTHROPIC_AUTH_TOKEN` to the LiteLLM token | It replaces the Claude Max OAuth bearer token and breaks upstream pass-through |
| Let the LiteLLM database own models/MCP definitions | It would defeat the reviewed YAML configuration source of truth |
| Apply emergency ConfigMap edits only in the cluster | It creates configuration drift and future rollouts can undo the emergency change |
| Build production images on Apple Silicon without an explicit target platform | ARM64 artifacts cannot run on AMD64 infrastructure |

## Validation Standard

- Configuration diff reviewed before apply
- Kubernetes rollout completes successfully
- Intended model or alias appears in the live `/v1/models` catalog
- One inexpensive live completion succeeds through the expected API surface
- Authorization changes are checked from both allowed and denied credentials where feasible
- Observability changes produce the expected isolated trace

## Safety Boundaries

- Never commit, print, or save API keys, tokens, `.env` values, or Kubernetes secret contents
- Never deploy a mutable image without an immutable, platform-compatible artifact and canary validation
- Never remove team scoping or fallbacks without explicitly evaluating cost and authorization impact
- Do not treat historical VM commands as the active GKE deployment process
