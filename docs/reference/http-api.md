# HTTP and gRPC API

Every endpoint the three services serve, the listener each is on, and the credential it needs. What a grant is, and how to turn internal authentication on, is in [Internal authentication](../operations/internal-auth.md).

| Service | Listener | Default | What it serves |
|---|---|---|---|
| gateway | public | `GATEWAY_ADDR`, `:5657` | webhooks from your Git platforms, the CLI API, health |
| gateway | admin | `ADMIN_ADDR`, `:9090` | health, metrics, OPA reload, pprof |
| gateway | permission engine | `AUTHZ_ADDR`, off unless set (e.g. `:5658`) | gRPC `Authz`, asked by the orchestrator |
| orchestrator | HTTP API | `ORCHESTRATOR_HTTP_ADDR`, `:8000` | webhooks, dry runs, tasks, schemas, the web UI's API, sign-in |
| orchestrator | gRPC | `:50051` | `Orchestrator`, `CLIService`, `Watcher` |
| orchestrator | admin | `ADMIN_HOST`:`ADMIN_PORT`, `127.0.0.1:9090` (the compose files use `9093`) | health, metrics, circuit breaker, pprof |
| watcher | admin | `ADMIN_ADDR`, `:9091` | health, metrics, a check on demand |

The credentials:

- **Webhook signature:** the platform's HMAC over the body, checked against the platform's `secret` in `config.cue`, and a delivery id that is refused if seen before. With `APP_ENV` other than `production` the gateway checks neither.
- **Git token:** `Authorization: Bearer <a token for the rule's Git platform>`. What the call may do is what that token may do in the rule's repositories ([Authentication & Authorization](../configuration/auth.md)).
- **Grant:** `Authorization: Bearer <grant>`, signed with `INTERNAL_AUTH_SECRET`, for one audience: `sync`, `watch`, `admin` or `authz`. With internal authentication off (development) no grant is asked for.
- **Session:** the web UI's signed-in session cookie.

---

## gateway, public listener (`:5657`)

| Method | Path | Needs | What |
|---|---|---|---|
| `POST` | `/webhooks/{datasource}` | webhook signature | A push or pull request event for the rule of the sending platform and `{datasource}`. Answers `202` with `{"task_id": "…", "message": "…"}`; `413` for a body over `max_webhook_body_bytes`; `503` when the orchestrator or the replay store is unavailable |
| `POST` | `/webhooks/schema-registry` | webhook signature | A push to a tenant's schema repository: its rules are read again |
| `GET` | `/health/liveness`, `/health/readiness` | nothing | Probes |
| `POST` | `/v1/cli/sync` | Git token | `vt sync`: sync a file (or dry-run it) |
| `POST` | `/v1/cli/validate` | Git token | `vt validate`: check a file against its schema |
| `POST` | `/v1/cli/validate-config` | Git token | `vt validate-config`: check a `vartrack.json` or `bundle.json` |
| `GET` | `/v1/cli/tasks`, `/v1/cli/tasks/{id}` | Git token | `vt task list`, `vt task get` |

The event header the platform sends (`X-GitHub-Event`, `X-Gitea-Event`) says what the delivery is; its delivery header (`X-GitHub-Delivery`, `X-Gitea-Delivery`) is what replay protection remembers.

## gateway, admin listener (`:9090`)

Keep it off the public network ([Production checklist](../operations/production-checklist.md)).

| Method | Path | What |
|---|---|---|
| `GET` | `/healthz`, `/health/liveness`, `/health/readiness` | Probes |
| `GET` | `/metrics` | Prometheus metrics |
| `POST` | `/admin/opa/reload` | Read the OPA policy file again |
| `GET` | `/debug/pprof/…` | Go profiling |

## orchestrator, HTTP API (`:8000`)

| Method | Path | Needs | What |
|---|---|---|---|
| `GET` | `/v1/health`, `/v1/health/liveness`, `/v1/health/readiness` | nothing | Probes; readiness names each dependency that is down or stale |
| `POST` | `/v1/webhooks/{datasource}` | `sync` grant | Queue a sync of a push, as the gateway does. Answers `{"task_id", "message", "received_at"}` |
| `POST` | `/v1/webhooks/{datasource}/dry-run` | `sync` grant | The same sync, writing nothing: what would be written and pruned, with `@secret` values masked. `?wait=true` (the default) answers with the report |
| `POST` | `/v1/webhooks/schema-registry` | `sync` grant | Read a tenant's schema repository again |
| `GET` | `/v1/tasks/{task_id}` | `sync` grant or session | A task's state (`PENDING`, `PROGRESS`, `RETRY`, `SUCCESS`) and, once it has finished, its result: `SUCCEEDED` or `FAILED`, the files and what happened to each. A task that raised answers `500` with the error |
| `GET` | `/v1/tenants/{tenant}/schemas` | `sync` grant or session | The schemas the tenant's schema repository holds, at which commit |
| `POST` | `/v1/tenants/{tenant}/schemas/warm` | `sync` grant | Fetch the tenant's schema repository now; answers the rules loaded and the commit |
| `GET` | `/v1/auth/session`, `/v1/auth/login`, `/v1/auth/callback` | nothing | The web UI's sign-in with a Git platform |
| `POST` | `/v1/auth/logout` | session | Sign out |
| `GET` | `/v1/ui/rules`, `/v1/ui/rules/{platform}/{datasource}` | session | The rules the viewer may see, and one rule's environments |
| `GET` | `/v1/ui/rules/{platform}/{datasource}/targets/{env}/keys` | session | The keys an environment holds in its sink |
| `POST` | `/v1/ui/rules/{platform}/{datasource}/sync` | session | Sync or dry-run a rule from the UI |
| `DELETE` | `/v1/ui/rules/{platform}/{datasource}/targets/{env}` | session | Forget an environment the rule no longer has |
| `GET` | `/v1/ui/events`, `/v1/ui/tasks`, `/v1/ui/tasks/{task_id}` | session | The history and the task pages |
| `GET` | `/v1/ui/bundle` | session | The bundle as the UI shows it |
| `GET` | `/docs`, `/redoc`, `/openapi.json` | `sync` grant | The OpenAPI description of every route above |
| `GET` | `/metrics` | `sync` grant | Prometheus metrics of the API process |

A route not listed as open needs a grant: one added later is closed until someone opens it on purpose.

## orchestrator, gRPC (`:50051`)

| Service | Methods | Called by | Needs |
|---|---|---|---|
| `Orchestrator` | `ProcessWebhook`, `ProcessSchemaWebhook`, `GetWebhookTask`, `Ping` | the gateway | `sync` grant |
| `CLIService` | `SyncFile`, `ValidateFile`, `ValidateConfig`, `GetTask`, `ListTasks` | the gateway, for the CLI API | `sync` grant |
| `Watcher` | `ListWatchTargets`, `CheckDatasource`, `SyncDatasource`, `Ping` | the watcher | `watch` grant |

The definitions are in `proto/vartrack/v1/services/`. With `MTLS_ENABLED` and `TLS_CA_FILE` the listener also asks for a client certificate.

## orchestrator, admin listener (`ADMIN_PORT`)

| Method | Path | Needs | What |
|---|---|---|---|
| `GET` | `/healthz`, `/health/liveness`, `/health/readiness` | nothing | Probes |
| `GET` | `/metrics` | `admin` grant | Prometheus metrics of the process (the API, or a worker) |
| `GET` | `/circuit-breaker` | `admin` grant | The state of the circuit breaker |
| `GET` | `/debug/pprof…` | `admin` grant | Python profiling |

[Scraping the admin port](../operations/internal-auth.md#scraping-the-admin-port) says how to mint the long-lived grant a Prometheus server uses.

## gateway, permission engine (`AUTHZ_ADDR`)

gRPC `Authz` (`Identify`, `Decide`, `ReadableRules`, `Forget`): what a signed-in web UI user may see and do, asked by the orchestrator with an `authz` grant. See [the permission engine](../operations/internal-auth.md#the-permission-engine).

## watcher, admin listener (`:9091`)

| Method | Path | Needs | What |
|---|---|---|---|
| `GET` | `/healthz`, `/health/liveness`, `/health/readiness` | nothing | Probes |
| `GET` | `/metrics` | nothing | Prometheus metrics |
| `POST` | `/admin/check` | `admin` grant | Check a rule now, and heal it if it drifted |
