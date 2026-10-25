# Environment variables

Every variable each service reads, with its default. Most settings are in
[`config.cue`](../configuration/bundle.md); the environment holds what a service needs before
it can read it, and what differs between hosts.

All three services also read a `.env` file without overriding variables already set: the
one named by `ENV_FILE`, else `.env` in the working directory (the orchestrator also looks in
the directories above its code).

---

## gateway-service

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `test` | `production` checks webhook signatures and replays and requires TLS. **Any other value skips both checks** |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARN`, `ERROR` |
| `CONFIG_PATH` | `config.cue` | The bundle; read once, at startup, strictly: an unknown field stops the gateway |
| `GATEWAY_ADDR` | `:5657` | Webhook and CLI API listener |
| `ADMIN_ADDR` | `:9090` | Health, metrics, pprof, OPA reload |
| `ORCHESTRATOR_ADDR` | `localhost:50051` | The orchestrator's gRPC address |
| `GATEWAY_TLS_CERT`, `GATEWAY_TLS_KEY` | — | Inbound TLS; required in production |
| `GRPC_TLS_CERT`, `GRPC_TLS_KEY` | — | Client certificate for mTLS to the orchestrator; required in production |
| `GRPC_TLS_CA` | — | CA to verify the orchestrator's certificate |
| `OPA_RELOAD_INTERVAL` | `30m` | How often an `auth.opa.policy_file` is re-read |
| `OTEL_ENABLED`, `OTEL_ENDPOINT` (`otel-collector:4317`), `OTEL_SERVICE_NAME` (`vartrack-gateway`), `OTEL_SERVICE_VERSION`, `OTEL_ENVIRONMENT` (`demo`) | off | Traces |
| `ELK_ENABLED`, `ES_ENDPOINTS` (`http://elasticsearch:9200`), `ES_INDEX` (`vartrack-logs`), `ELK_SERVICE_NAME`, `ELK_ENVIRONMENT` | off | Log shipping to Elasticsearch |
| `POD_IP` | — | Added to the addresses of the self-signed certificate the gateway makes when it has none (Kubernetes downward API) |

The replay store for several replicas is set in `config.cue` (`gateway_nonce_datasource`),
not in the environment — see [High availability](../operations/high-availability.md#gateway).

## orchestrator-service (API and workers)

Set the same values on the API and on every Celery worker.

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `development` | `production` turns mTLS on unless `MTLS_ENABLED` says otherwise |
| `LOG_LEVEL` | `INFO` | |
| `CONFIG_PATH` | — | The bundle; re-read when the file changes |
| `ORCHESTRATOR_ADDR` | `0.0.0.0:8000` | HTTP listener (API and web UI read API) |
| `GRPC_PORT` | `50051` | gRPC listener (webhooks from the gateway, the CLI API, the watcher) |
| `ADMIN_HOST`, `ADMIN_PORT` | `0.0.0.0`, `9090` | Health and metrics |
| `CORS_ORIGINS` | — | Comma-separated origins the web UI is served from; empty allows none, `*` any |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | Celery broker; also the Redis that holds the sync ledger, the locks and the UI's tasks and events |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/0` | Celery results |
| `GIT_CACHE_DIR` | `/tmp/vt_gitcache` | Bare mirrors of the config repositories; share it between the API and the workers of a host |
| `GIT_CACHE_MAX_ENTRIES` | `20` | Mirrors kept; the least recently used go first, never one in use |
| `SCHEMA_CACHE_DIR` | `/tmp/schema_registry` | Clones of the tenants' schema repositories; share it too |
| `SCHEMA_TTL_SECONDS` | `300` | How often a schema repository is refreshed |
| `SCHEMA_TENANT_<ID>_REPO`, `_BRANCH` (`main`), `_TOKEN` | — | Registers tenant `<id>` and its schema repository — see [Add a tenant](../guides/add-a-tenant.md) |
| `GIT_ALLOWED_HOSTS` | — | Comma-separated Git hosts a schema repository may be on although they resolve to a private or loopback address (e.g. `gitea` in Docker); others in those ranges are refused |
| `MTLS_ENABLED` | `false` (`true` in production) | TLS on HTTP and gRPC |
| `TLS_CERT_FILE`, `TLS_KEY_FILE` | — | The server certificate |
| `TLS_CA_FILE` | — | When set, clients must present a certificate it signed |
| `TLS_SELF_SIGNED` | `false` | Generate a self-signed certificate at startup (development, CI) |
| `CUE_BINARY` | `cue` | The CUE binary used to read the bundle and validate files |
| `CUE_TIMEOUT_SECONDS` | `15` | Time limit of a `cue` run |
| `CUE_MIN_VERSION` | `v0.6.0` | Oldest `cue` accepted |
| `HELM_BINARY` | `helm` on the `PATH` | For the Helm sink |
| `MONGO_URI`, `MONGO_TLS`, `MONGO_TLS_CA_FILE`, `MONGO_TLS_INSECURE` | `mongodb://localhost:27017` | Fallback for a Mongo datasource whose `config.cue` block sets no endpoint |
| `UI_DEFAULT_TENANT` | `default` | The tenant the web UI shows when none is registered |
| `UI_COMPARE_TTL_SECONDS` | `10` | How long the web UI reuses a datasource read when comparing it with Git |
| `UI_PUBLIC_URL`, `UI_AUTH_PLATFORMS`, `UI_AUTH_<P>_CLIENT_ID`, `_CLIENT_SECRET`, `_URL`, `_SCOPES`, `UI_AUTH_REDIRECT_URL`, `UI_AUTH_ADMINS`, `UI_SESSION_TTL_SECONDS`, `UI_SESSION_SECRET` | — | Web UI sign-in when `config.cue` has no `ui.auth` block; each can be read from a file with `_FILE` — see [Web UI sign-in](../configuration/auth.md#set-it-up) |
| `APP_VERSION`, `GIT_COMMIT` | `dev`, `unknown` | Build metadata, set by the image |

### Observability (orchestrator)

| Prefix | Turned on by | Main settings |
|---|---|---|
| `PROM_` | `PROM_ENABLED=true` | `PROM_PORT` (`9090`), `PROM_METRICS_PATH` (`/metrics`), `PROM_EXTERNAL_LABELS`; push to a gateway with `PROM_PUSH_ENABLED`, `PROM_PUSH_URL`, `PROM_PUSH_INTERVAL` (15 s), `PROM_JOB_NAME`; TLS and basic auth with `PROM_ENABLE_TLS`, `PROM_TLS_*`, `PROM_BASIC_AUTH_USER`/`_PASS` |
| `OTEL_` | `OTEL_ENABLED=true` | `OTEL_ENDPOINT` (`localhost:4317`), `OTEL_PROTOCOL` (`grpc`), `OTEL_SERVICE_NAME`, `OTEL_ENVIRONMENT`, `OTEL_SAMPLER_TYPE`/`_RATIO`, `OTEL_PROPAGATORS` (`tracecontext,baggage`), `OTEL_ENABLE_TRACES`, `OTEL_ENABLE_METRICS`, `OTEL_HEADERS`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_USE_TLS` and `OTEL_TLS_*` |
| `JAEGER_` | `JAEGER_ENABLED=true` | `JAEGER_ENDPOINT` (`localhost:4317`), `JAEGER_PROTOCOL`, `JAEGER_SERVICE_NAME`, `JAEGER_SAMPLER_TYPE`/`_PARAM`, `JAEGER_AUTH_TOKEN`, `JAEGER_USE_TLS` and `JAEGER_TLS_*` |
| `ES_`, `ELK_` | `ELK_ENABLED=true` | `ES_ENDPOINTS` (`http://localhost:9200`), `ES_INDEX` (`vartrack-logs`), `ES_USERNAME`/`ES_PASSWORD`, `ES_API_KEY` or `ES_BEARER_TOKEN`, `ES_BULK_MAX_DOCS` (500), `ES_FLUSH_INTERVAL` (5 s), `ES_PIPELINE`, `ES_TLS_*` |
| `LOGSTASH_` | `LOGSTASH_ENDPOINT` set | `LOGSTASH_TIMEOUT` (10 s), `LOGSTASH_USE_TLS` and `LOGSTASH_TLS_*` |

## watcher-service

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `development` | `production` requires the TLS files below |
| `LOG_LEVEL` | `INFO` | |
| `CONFIG_PATH` | `config.cue` | Read once, for `watcher_leader_election_datasource` only |
| `ORCHESTRATOR_ADDR` | `localhost:50051` | The orchestrator's gRPC address |
| `ADMIN_ADDR` | `:9091` | Health and metrics |
| `POLL_INTERVAL` | `60s` | How often each rule is checked for drift |
| `HEAL_TIMEOUT` | `30s` | Time limit of one heal request |
| `TLS_CA_FILE`, `TLS_CERT_FILE`, `TLS_KEY_FILE` | — | mTLS to the orchestrator; required in production |
| `OTEL_ENABLED`, `ELK_ENABLED` | off | Traces and log shipping, as on the gateway |

## vt (CLI)

| Variable | Description |
|---|---|
| `VARTRACK_SERVER` | The gateway's URL; with `VARTRACK_TOKEN`, used instead of the stored context (CI) |
| `VARTRACK_TOKEN` | A Git personal access token; overrides the stored one |
| `VARTRACK_TENANT` | Default for `--tenant` |

The stored contexts are in `~/.config/vt/config.yaml` (`--config` to use another file) —
see [vt commands](../cli/index.md).
