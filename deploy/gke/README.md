# LiteLLM gateway on GKE

Manifests for the LiteLLM proxy running on GKE in the `litellm` namespace.

- `redis.yaml`: in-cluster Redis (Deployment + ClusterIP Service). LiteLLM uses it for
  cross-replica router coordination (shared rate limits and cooldowns) and response caching.
- `litellm-config.yaml`: the LiteLLM `config.yaml` mounted through the `litellm-config`
  ConfigMap: model_list, aliases, MCP servers, and the Redis wiring under `router_settings`
  and `litellm_settings`.

Secrets are never committed. Create them in-cluster:
- `litellm-redis` with key `REDIS_PASSWORD`, consumed by both Redis and the proxy
- `litellm-secrets` with the provider API keys, master key, and DB credentials

Apply:

    kubectl -n litellm apply -f redis.yaml
    kubectl -n litellm create configmap litellm-config \
      --from-file=config.yaml=litellm-config.yaml --dry-run=client -o yaml | kubectl -n litellm apply -f -
    kubectl -n litellm set env deployment/litellm --from=secret/litellm-redis --containers=litellm
    kubectl -n litellm rollout restart deployment/litellm
