# LiteLLM Gateway Operations

This directory is the versioned source of truth for the LiteLLM gateway configuration running in the `litellm` namespace on GKE.

## Source Of Truth

- Change `litellm-config.yaml` for models, model aliases, fallbacks, callbacks, MCP servers, and proxy settings.
- Change `redis.yaml` for the in-cluster Redis deployment and service.
- Secrets are created and maintained only in Kubernetes. Do not add secret values, `.env` files, or credentials to this repository or documentation.
- Do not patch the live ConfigMap as the only change. If an emergency live change is unavoidable, reconcile it into `litellm-config.yaml` before the next rollout.

## Apply A Configuration Change

From this directory, apply Redis when it changes, regenerate the ConfigMap from the YAML file, then restart LiteLLM:

```bash
kubectl -n litellm apply -f redis.yaml
kubectl -n litellm create configmap litellm-config \
  --from-file=config.yaml=litellm-config.yaml --dry-run=client -o yaml | kubectl -n litellm apply -f -
kubectl -n litellm rollout restart deployment/litellm
kubectl -n litellm rollout status deployment/litellm
```

Before a model-related rollout, validate the YAML structure and inspect the intended diff. After it, validate the live model catalog and exercise an inexpensive real completion through the gateway. Do not expose API keys in shell history, logs, commits, or agent responses.

## Claude Code Model Discovery

Claude Code gateway discovery only shows model IDs beginning with `claude` or `anthropic`. Non-Claude chat models therefore need aliases under `router_settings.model_group_alias` that use one of those prefixes and map to the real LiteLLM model group. Do not alias transcription, embeddings, image generation, or video generation because Claude Code cannot use them as its chat model.

The current aliases use names such as `claude-g.p.t-*` and `claude-g.emini-*`. Keep the real group name on the right side of each mapping. This avoids duplicating provider credentials or model definitions.

Claude Code sends the `[1m]` suffix literally when a 1M-context option is selected. A gateway alias must map each supported suffixed identifier to the unsuffixed model group, otherwise LiteLLM forwards an invalid provider model ID.

For Claude Max OAuth pass-through clients, proxy authentication must use a separate custom LiteLLM header. Do not configure the client with a proxy `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY`: either would replace the Claude subscription OAuth bearer token. `general_settings.forward_client_headers_to_llm_api: true` preserves the upstream OAuth path for Claude models.

## Access, Fallbacks, And Configuration Storage

- A deployment with `model_info.team_id` is visible only to keys in the matching team. Use team scoping, not access groups, to restrict expensive models.
- A caller without the required team receives `429 No deployments available for selected model`. A fallback to a team-scoped model is likewise unavailable to unauthorized callers.
- Keep models and MCP server definitions in YAML. `store_model_in_db: true` enables UI-managed settings, while `supported_db_objects` intentionally omits `models` and `mcp`.
- Add fallbacks only after confirming the fallback model is available to every caller that may need it.

## Redis And Langfuse

Redis provides shared router coordination and response caching for the LiteLLM replicas. The config references the Kubernetes service name `redis` and receives its password from `litellm-redis`.

Langfuse callbacks are enabled in `litellm_settings`. Credentials and trace labels remain Kubernetes secrets or deployment environment configuration. To isolate gateway telemetry, use the established LiteLLM tracing environment and release labels rather than changing application-wide Langfuse credentials.

## Deployment Safety

- Build gateway images for `linux/amd64`; the previous VM deployment attempt failed because a Mac-built ARM64 image could not run on AMD64 infrastructure.
- Use immutable image tags for production builds. Promote mutable tags only after the immutable image passes a config canary, liveness check, and real completion.
- The GitHub deploy workflow is VM-oriented historical automation. The current gateway is GKE; do not treat that workflow as the deployment path for these manifests without explicitly migrating it.
- Preserve the existing team IDs and external endpoint configuration unless the task explicitly requires changing them.

## Historical Context

`SESSION-020043EB-HISTORY.md` documents why the aliasing, OAuth pass-through, Langfuse, database-storage, and image-platform constraints exist. Read it before changing this configuration when the rationale is not obvious.
