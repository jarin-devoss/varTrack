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
| `INTERNAL_AUTH_SECRET` (or `INTERNAL_AUTH_SECRET_FILE`) | — | Signs the grants the gateway presents to the orchestrator; 32+ characters, the same on every service. Required in production — see [Internal authentication](../operations/internal-auth.md) |
| `INTERNAL_AUTH_INSECURE` | `false` | Production only: start without `INTERNAL_AUTH_SECRET` on purpose |
| `AUTHZ_ADDR` | — | Where the permission engine is served to the orchestrator's web UI (e.g. `:5658`). Unset serves nothing: the CLI API still asks the engine in-process, but a web UI with sign-in on then answers `503`. In production it needs `INTERNAL_AUTH_SECRET` and `GATEWAY_TLS_CERT`/`GATEWAY_TLS_KEY`. See [Internal authentication](../operations/internal-auth.md#the-permission-engine) |
| `INTERNAL_AUTH_AUTHZ_SECRET` (or `*_FILE`) | `INTERNAL_AUTH_SECRET` | Optional separate key for the grants the orchestrator presents to `AUTHZ_ADDR`; 32+ characters |
| `AUTHZ_CACHE_TTL` | `60s` | How long the engine remembers what a Git platform said (a duration; `0` turns the cache off). A revoked permission keeps working for up to this long |

The replay store for several replicas is set in `config.cue` (`gateway_nonce_datasource`),
not in the environment — see [High availability](../operations/high-availability.md#gateway).

## orchestrator-service (API and workers)

Set the same values on the API and on every Celery worker.

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `development` | `production` turns mTLS on unless `MTLS_ENABLED` says otherwise |
| `LOG_LEVEL` | `INFO` | |
| `CONFIG_PATH` | — | The bundle; re-read when the file changes |
| `ORCHESTRATOR_HTTP_ADDR` | `0.0.0.0:8000` | HTTP listener (API and web UI read API). Formerly `ORCHESTRATOR_ADDR`, which still works here for one release with a warning: everywhere else that name is the orchestrator's gRPC address to dial |
| `GRPC_PORT` | `50051` | gRPC listener (webhooks from the gateway, the CLI API, the watcher) |
| `ADMIN_HOST`, `ADMIN_PORT` | `127.0.0.1`, `9090` | Health and metrics. Loopback by default; with `INTERNAL_AUTH_SECRET` set, everything but the health probes needs an admin grant |
| `ADMIN_ENABLE_PPROF` | `DEBUG` | Serves `/debug/pprof` on the admin listener |
| `VARTRACK_BUNDLE_BINARY` | `vartrack-bundle` on the `PATH` | The program that reads `config.cue` for the orchestrator exactly as the gateway does (`go build ./cmd/vartrack-bundle` in `libs/go`; the image ships it). Without it the orchestrator cannot load its bundle, and a production one does not start |
| `CORS_ORIGINS` | — | Comma-separated origins the web UI is served from; empty allows none. `*` allows any only while running open for development: it is ignored in production or once `INTERNAL_AUTH_SECRET` is set |
| `INTERNAL_AUTH_SECRET` (or `INTERNAL_AUTH_SECRET_FILE`) | — | Verifies the signed grants every call to the HTTP API, the gRPC port and the admin port must carry; 32+ characters, the same on the gateway and the watcher. Without it the orchestrator runs open (development), and in production it refuses to start. See [Internal authentication](../operations/internal-auth.md) |
| `INTERNAL_AUTH_WATCH_SECRET`, `INTERNAL_AUTH_ADMIN_SECRET` (or `*_FILE`) | `INTERNAL_AUTH_SECRET` | Optional separate keys for the watcher's grants and the admin listener's |
| `INTERNAL_AUTH_INSECURE` | `false` | Production only: run without `INTERNAL_AUTH_SECRET` on purpose |
| `GATEWAY_AUTHZ_ADDR` | — | `host:port` of the gateway's `AUTHZ_ADDR`: the permission engine the web UI asks what a signed-in user may see and do. Unset with sign-in on, the UI answers `503` rather than deciding itself |
| `GATEWAY_AUTHZ_CA_FILE`, `GATEWAY_AUTHZ_TIMEOUT` | `TLS_CA_FILE`, `10` | CA that signed the gateway's certificate (the client certificate is `TLS_CERT_FILE`/`TLS_KEY_FILE`), and seconds to wait for an answer |
| `INTERNAL_AUTH_AUTHZ_SECRET` (or `*_FILE`) | `INTERNAL_AUTH_SECRET` | Optional separate key for the grants this service presents to the gateway's engine |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | Celery broker; also the Redis that holds the sync ledger, the locks and the UI's tasks and events |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/0` | Celery results |
| `GIT_CACHE_DIR` | `/tmp/vt_gitcache` | Bare mirrors of the config repositories; share it between the API and the workers of a host |
| `GIT_CACHE_MAX_ENTRIES` | `20` | Mirrors kept; the least recently used go first, never one in use |
| `SCHEMA_CACHE_DIR` | `/tmp/schema_registry` | Clones of the tenants' schema repositories; share it too |
| `SCHEMA_TTL_SECONDS` | `300` | How often a schema repository is refreshed |
| `SCHEMA_TENANT_<ID>_REPO`, `_BRANCH` (`main`), `_TOKEN` | — | **Deprecated**: registers tenant `<id>` and its schema repository, with a warning. Declare the tenant in `config.cue`'s [`tenants`](../configuration/bundle.md#tenants) instead — see [Add a tenant](../guides/add-a-tenant.md) |
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
| `UI_PUBLIC_URL`, `UI_AUTH_PLATFORMS`, `UI_AUTH_<P>_CLIENT_ID`, `_CLIENT_SECRET`, `_URL`, `_SCOPES`, `UI_AUTH_REDIRECT_URL`, `UI_SESSION_TTL_SECONDS`, `UI_SESSION_SECRET` | — | Web UI sign-in when `config.cue` has no `ui.auth` block; each can be read from a file with `_FILE` — see [Web UI sign-in](../configuration/auth.md#set-it-up). The admins are `ui.auth.admins` in the bundle; `UI_AUTH_ADMINS` is no longer read, and the orchestrator refuses to start while it is set |
| `APP_VERSION`, `GIT_COMMIT` | `dev`, `unknown` | Build metadata, set by the image |

### Observability (orchestrator)

| Prefix | Turned on by | Main settings |
|---|---|---|
| `PROM_` | `PROM_ENABLED=true` | `PROM_PORT` (`9090`), `PROM_METRICS_PATH` (`/metrics`), `PROM_EXTERNAL_LABELS`; push to a gateway with `PROM_PUSH_ENABLED`, `PROM_PUSH_URL`, `PROM_PUSH_INTERVAL` (15 s), `PROM_JOB_NAME`; TLS and basic auth with `PROM_ENABLE_TLS`, `PROM_TLS_*`, `PROM_INSECURE_SKIP_VERIFY`, `PROM_BASIC_AUTH_USER`, `PROM_BASIC_AUTH_PASS` |
| `OTEL_` | `OTEL_ENABLED=true` | `OTEL_ENDPOINT` (`localhost:4317`), `OTEL_PROTOCOL` (`grpc`), `OTEL_SERVICE_NAME`, `OTEL_ENVIRONMENT`, `OTEL_SAMPLER_TYPE`, `OTEL_SAMPLER_RATIO`, `OTEL_PROPAGATORS` (`tracecontext,baggage`), `OTEL_ENABLE_TRACES`, `OTEL_ENABLE_METRICS`, `OTEL_HEADERS`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_BATCH_TIMEOUT`, `OTEL_MAX_QUEUE_SIZE`, `OTEL_MAX_EXPORT_BATCH` (0 leaves the SDK's own), `OTEL_METRICS_INTERVAL` (60 s), `OTEL_USE_TLS`, `OTEL_INSECURE_SKIP_VERIFY` and `OTEL_TLS_*` |
| `JAEGER_` | `JAEGER_ENABLED=true` | `JAEGER_ENDPOINT` (`localhost:4317`), `JAEGER_PROTOCOL`, `JAEGER_SERVICE_NAME`, `JAEGER_SAMPLER_TYPE`, `JAEGER_SAMPLER_PARAM`, `JAEGER_AUTH_TOKEN`, `JAEGER_SERVICE_VERSION`, `JAEGER_ENVIRONMENT`, `JAEGER_RESOURCE_ATTRIBUTES`, `JAEGER_MAX_QUEUE_SIZE`, `JAEGER_FLUSH_INTERVAL` (0 leaves the SDK's own), `JAEGER_USE_TLS`, `JAEGER_INSECURE_SKIP_VERIFY` and `JAEGER_TLS_*` |
| `ES_`, `ELK_` | `ELK_ENABLED=true` | `ES_ENDPOINTS` (`http://localhost:9200`), `ES_INDEX` (`vartrack-logs`), `ES_USERNAME`/`ES_PASSWORD`, `ES_API_KEY` or `ES_BEARER_TOKEN`, `ES_BULK_MAX_DOCS` (500), `ES_FLUSH_INTERVAL` (5 s), `ES_PIPELINE`, `ES_EXTRA_FIELDS`, `ELK_SERVICE_VERSION`, `ES_INSECURE_SKIP_VERIFY` and `ES_TLS_*` |
| `LOGSTASH_` | `LOGSTASH_ENDPOINT` set | `LOGSTASH_TIMEOUT` (10 s), `LOGSTASH_USE_TLS`, `LOGSTASH_INSECURE_SKIP_VERIFY` and `LOGSTASH_TLS_*` |

## watcher-service

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `development` | `production` requires the TLS files below |
| `VARTRACK_ALLOW_DEGRADED` | — | Comma-separated weaker modes the operator accepts: `config` (start on environment variables when `config.cue` cannot be read), `nonce_store` (replay protection in-process instead of Redis), `tls` (no TLS when there is no certificate), `schema_validation` (write files unvalidated when the `cue` program is missing). Each is logged when taken and listed by `/health/readiness` while on. `nonce_store` and `tls` are refused in production. Without it, none of these is ever a fallback: the service stops, or the request fails |
| `LOG_LEVEL` | `INFO` | |
| `CONFIG_PATH` | `config.cue` | Read once, for `watcher_leader_election_datasource` only |
| `ORCHESTRATOR_ADDR` | `localhost:50051` | The orchestrator's gRPC address |
| `ADMIN_ADDR` | `:9091` | Health and metrics |
| `POLL_INTERVAL` | `60s` | How often each rule is checked for drift |
| `HEAL_TIMEOUT` | `30s` | Time limit of one heal request |
| `TLS_CA_FILE`, `TLS_CERT_FILE`, `TLS_KEY_FILE` | — | mTLS to the orchestrator; required in production |
| `INTERNAL_AUTH_WATCH_SECRET`, else `INTERNAL_AUTH_SECRET` (or `*_FILE`) | — | Signs the grants the watcher presents to the orchestrator; required in production, and the same key the orchestrator verifies `watch` grants with |
| `INTERNAL_AUTH_INSECURE` | `false` | Production only: start without a secret on purpose |
| `OTEL_ENABLED`, `ELK_ENABLED` | off | Traces and log shipping, as on the gateway |

## vt (CLI)

| Variable | Description |
|---|---|
| `VARTRACK_SERVER` | The gateway's URL; with `VARTRACK_TOKEN`, used instead of the stored context (CI) |
| `VARTRACK_TOKEN` | A Git personal access token; overrides the stored one |
| `VARTRACK_TENANT` | Default for `--tenant` |

The stored contexts are in `~/.config/vt/config.yaml` (`--config` to use another file) —
see [vt commands](../cli/index.md).
